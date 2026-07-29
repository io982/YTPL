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
        logger.warning(f"Error fetching page text: {e}")
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
    pattern2 = r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s]*=(\d+s)'
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

def detect_silence_timestamps(audio_path, duration, noise_tol='-30dB', min_silence_dur=1.5):
    """
    Detect silence gaps in audio using ffmpeg silencedetect and return split timestamps
    at the midpoints of each silence region. Useful for splitting mix/album uploads
    that have no chapter markers.
    
    Parameters:
        audio_path     — path to audio file
        duration       — total audio duration in seconds (silence beyond this is ignored)
        noise_tol      — noise tolerance in dB (default: -30dB, range: -50 to -20)
        min_silence_dur — minimum silence duration in seconds to treat as a track gap (default: 1.5)
    
    Returns:
        Sorted list of timestamps (seconds) at silence-gap midpoints.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    logger.info(f"Запуск silencedetect (шум={noise_tol}, мин. пауза={min_silence_dur}s)...")

    cmd = [
        ffmpeg_path,
        '-i', audio_path,
        '-af', f'silencedetect=n={noise_tol}:d={min_silence_dur}',
        '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    silence_starts = []
    silence_ends = []

    for line in result.stderr.split('\n'):
        if 'silence_start' in line:
            m = re.search(r'silence_start:\s*([\d.]+)', line)
            if m:
                val = float(m.group(1))
                silence_starts.append(val)
        if 'silence_end' in line:
            m = re.search(r'silence_end:\s*([\d.]+)', line)
            if m:
                val = float(m.group(1))
                silence_ends.append(val)

    # Pair starts and ends: ffmpeg outputs them in alternating order.
    # Ignore silence that starts at 0.0 (leading silence).
    timestamps = []
    pairs = min(len(silence_starts), len(silence_ends))
    for i in range(pairs):
        s_start = silence_starts[i]
        s_end = silence_ends[i]
        # Skip leading silence
        if s_start <= 0.1:
            continue
        midpoint = (s_start + s_end) / 2
        if 0 < midpoint < duration - 1.0:  # not too close to the end
            timestamps.append(midpoint)

    logger.info(f"silencedetect: найдено тихих промежутков — {pairs}, "
                f"валидных точек разреза — {len(timestamps)}")
    return sorted(timestamps)


def download_audio(url):
    """
    Download audio from YouTube video using yt-dlp.
    Returns path to downloaded MP3 file, title, description, duration, temp_dir_path.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    temp_dir_path = tempfile.mkdtemp(prefix='ytpl_audio_')
    outtmpl = os.path.join(temp_dir_path, 'audio.%(ext)s')
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': False,
        'no_warnings': False,
        'socket_timeout': 60,       # увеличенный таймаут сокета (было 20 по умолчанию)
        'retries': 10,              # количество повторных попыток при ошибке загрузки
        'fragment_retries': 10,     # повторные попытки для фрагментированных потоков
        'http_chunk_size': 10485760, # 10 МБ чанки — помогают при нестабильном соединении
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
        
        if ext.lower() == 'mp3':
            mp3_file = downloaded_file  # Уже MP3, используем как есть
        else:
            mp3_file = os.path.join(temp_dir_path, 'audio.mp3')
            cmd = [ffmpeg_path, '-i', downloaded_file, '-q:a', '0', '-y', mp3_file]
            subprocess.run(cmd, check=True)
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

    try:
        # Download audio (получаем title, description, duration вместе с файлом)
        logger.info("Загрузка аудио...")
        audio_path, title, description, duration, temp_dir_path = download_audio(url)
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
            logger.info("Метки времени не найдены. Запускаю анализ пауз (silence detection)...")
            timestamps = detect_silence_timestamps(audio_path, duration)

        if not timestamps:
            cont = input("No timestamps found. Continue with download? (y/n): ").strip().lower()
            if cont != 'y':
                return []

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
        print(f"\n[ОШИБКА] {e}")
        print("Возможные решения:")
        print("  1. Проверьте подключение к интернету")
        print("  2. Обновите yt-dlp: pip install -U yt-dlp")
        print("  3. Обновите imageio-ffmpeg: pip install -U imageio-ffmpeg")
        print("  4. Попробуйте другую ссылку на YouTube")
