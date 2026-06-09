import yt_dlp
import re
import os
import subprocess
import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup
import tempfile
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def parse_time(time_str):
    """
    Parse time string into seconds. Supports formats: 145s, 2m25s, 1:30, 1:30:25
    """
    time_str = time_str.strip()
    if ':' in time_str:
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
    elif 'm' in time_str:
        parts = time_str.split('m')
        minutes = int(parts[0])
        seconds = 0
        if len(parts) > 1 and parts[1].endswith('s'):
            seconds = int(parts[1][:-1])
        return minutes * 60 + seconds
    elif time_str.endswith('s'):
        return int(time_str[:-1])
    else:
        return int(time_str)  # assume seconds

def get_full_page_text(url):
    """
    Get full text from YouTube page HTML for timestamp extraction.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        return soup.get_text()
    except Exception as e:
        print(f"Error fetching page text: {e}")
        return ""

def extract_timestamps(text):
    """
    Extract timestamps from text (description or page HTML).
    Supports formats: &t=145s, ?t=145s, =1102s in video URLs, MM:SS, HH:MM:SS, etc.
    """
    timestamps = []
    # Regex for ?t= or &t= patterns
    pattern1 = r'[?&]t=([^&\s]+)'
    matches1 = re.findall(pattern1, text)
    for match in matches1:
        try:
            sec = parse_time(match)
            if sec > 0:
                timestamps.append(sec)
        except ValueError:
            continue

    # Regex for video URL = time s patterns, e.g., https://youtu.be/...=1102s
    pattern2 = r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^&\s]+=([^&\s]+)'
    matches2 = re.findall(pattern2, text)
    for match in matches2:
        try:
            sec = parse_time(match)
            if sec > 0:
                timestamps.append(sec)
        except ValueError:
            continue

    # Regex for MM:SS or HH:MM:SS patterns in text
    pattern3 = r'\b\d{1,2}:\d{2}(?::\d{2})?\b'
    matches3 = re.findall(pattern3, text)
    for match in matches3:
        try:
            sec = parse_time(match)
            if sec > 0:
                timestamps.append(sec)
        except ValueError:
            continue

    return sorted(set(timestamps))

def download_audio(url, temp_dir='temp_audio'):
    """
    Download audio from YouTube video using yt-dlp.
    Returns path to downloaded MP3 file, title, description, duration.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    # Создаём временную директорию для загрузки
    temp_dir_path = tempfile.mkdtemp(prefix='ytpl_audio_')
    outtmpl = os.path.join(temp_dir_path, 'audio.%(ext)s')
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': False,
        'no_warnings': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['web', 'android', 'ios'],
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info['title']
        description = info.get('description', '')
        duration = info['duration']
        ext = info.get('ext', 'm4a')
        
        # Находим скачанный файл
        downloaded_file = None
        for f in os.listdir(temp_dir_path):
            downloaded_file = os.path.join(temp_dir_path, f)
            break
        
        if not downloaded_file or not os.path.exists(downloaded_file):
            raise FileNotFoundError("Audio file was not downloaded successfully")
        
        mp3_file = os.path.join(temp_dir_path, 'audio.mp3')
        if ext.lower() == 'mp3':
            # Переименовываем в mp3 для единообразия
            mp3_file = downloaded_file
            os.rename(downloaded_file, mp3_file)
        else:
            # Convert to mp3 using ffmpeg
            cmd = [ffmpeg_path, '-i', downloaded_file, '-q:a', '0', '-y', mp3_file]
            subprocess.run(cmd, check=True)
            # Remove original
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)
    
    return mp3_file, title, description, duration, temp_dir_path

def split_audio(audio_path, timestamps, duration, base_name='видео', output_dir='.'):
    """
    Split audio into segments based on timestamps using ffmpeg.
    Returns list of created segment paths.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    segments = []
    created_files = []
    start = 0
    
    # Фильтруем и валидируем временные метки
    valid_timestamps = []
    for ts in timestamps:
        if ts <= start:
            logger.warning(f"Пропущена некорректная метка: {ts}s (должна быть > {start}s)")
            continue
        if ts > duration:
            logger.warning(f"Пропущена метка за пределами видео: {ts}s (длительность: {duration}s)")
            continue
        valid_timestamps.append(ts)
    
    timestamps = valid_timestamps
    
    for i, end in enumerate(timestamps):
        if end > start:
            segments.append((start, end))
            start = end
        else:
            logger.warning(f"Пропущен перекрывающийся сегмент: {start}s - {end}s")

    # Добавляем последний сегмент до конца видео
    if start < duration:
        segments.append((start, duration))
    elif start == duration and segments:
        logger.info("Последняя метка совпадает с концом видео, последний сегмент не нужен")
    elif not segments:
        logger.warning("Нет валидных сегментов для создания")

    logger.info(f"Создание {len(segments)} сегментов")
    
    for i, (start_time, end_time) in enumerate(segments):
        segment_num = f"{i+1:04d}"
        output_filename = os.path.join(output_dir, f"{base_name}_{segment_num}.mp3")
        duration_seg = end_time - start_time
        
        logger.info(f"Сегмент {i+1}/{len(segments)}: {start_time}s - {end_time}s ({duration_seg:.1f}s)")
        
        cmd = [ffmpeg_path, '-i', audio_path, '-ss', str(start_time), '-t', str(duration_seg), '-c', 'copy', output_filename]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Ошибка при создании сегмента {i+1}: {result.stderr}")
            continue
        
        if os.path.exists(output_filename):
            created_files.append(output_filename)
            logger.info(f"Создан: {output_filename}")
        else:
            logger.error(f"Файл сегмента не был создан: {output_filename}")

    return created_files

def process_youtube_video(url, manual_timestamps=None):
    """
    Main function to process YouTube video.
    Returns list of created segment paths.
    """
    temp_dir_path = None  # Для хранения пути к временной директории
    audio_path = None
    
    try:
        print("Fetching video information...")
        # Get description using yt-dlp
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            description = info.get('description', '')
            title = info['title']
            duration = info['duration']
        print(f"Video title: {title}")
        print(f"Video duration: {duration}s")

        # Extract timestamps
        if manual_timestamps:
            timestamps = [int(t) for t in manual_timestamps.split(',') if t.strip().isdigit()]
            print(f"Using manual timestamps: {timestamps}")
        else:
            timestamps = extract_timestamps(description)
            if not timestamps:
                page_text = get_full_page_text(url)
                timestamps = extract_timestamps(page_text)
            print(f"Extracted timestamps: {timestamps}")

        if not timestamps:
            cont = input("No timestamps found. Continue with download? (y/n): ").strip().lower()
            if cont != 'y':
                return []

        # Download audio
        logger.info("Загрузка аудио...")
        audio_path, _, _, _, temp_dir_path = download_audio(url)

        # Split audio
        base_name = re.sub(r'[^\w\-_\. ]', '_', title).replace(' ', '_')
        output_dir = base_name
        os.makedirs(output_dir, exist_ok=True)
        
        logger.info("Разделение аудио на сегменты...")
        created_segments = split_audio(audio_path, timestamps, duration, base_name, output_dir)

        print(f"\n{'='*50}")
        print(f"Обработка завершена успешно!")
        print(f"Создано сегментов: {len(created_segments)}")
        print(f"Папка вывода: {output_dir}/")
        print(f"{'='*50}")
        
        return created_segments

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        raise
    finally:
        # Очистка временных файлов
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                import shutil
                shutil.rmtree(temp_dir_path)
                logger.info(f"Временные файлы удалены: {temp_dir_path}")
            except Exception as cleanup_error:
                logger.warning(f"Не удалось удалить временные файлы: {cleanup_error}")

if __name__ == "__main__":
    # Проверка наличия ffmpeg
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        if not os.path.exists(ffmpeg_path):
            raise RuntimeError(f"FFmpeg не найден по пути: {ffmpeg_path}")
        logger.info(f"FFmpeg найден: {ffmpeg_path}")
    except Exception as e:
        logger.error(f"FFmpeg не найден. Установите imageio-ffmpeg: pip install imageio-ffmpeg")
        exit(1)
    
    url = input("Enter YouTube video URL: ").strip()
    manual_input = input("Enter manual timestamps (comma-separated in seconds, e.g., '145,325'), or leave empty to extract from description: ").strip()

    manual_timestamps = manual_input if manual_input else None

    try:
        process_youtube_video(url, manual_timestamps)
    except KeyboardInterrupt:
        logger.info("\nОперация отменена пользователем")
    except Exception as e:
        logger.error(f"Произошла ошибка: {e}")
        exit(1)
