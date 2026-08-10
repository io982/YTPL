import yt_dlp
import re
import os
import math
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

def _run_silencedetect(audio_path, noise_tol, min_silence_dur):
    """
    Запускает ffmpeg silencedetect один раз и возвращает список пауз как
    кортежи (start, end) в секундах.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
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
                silence_starts.append(float(m.group(1)))
        if 'silence_end' in line:
            m = re.search(r'silence_end:\s*([\d.]+)', line)
            if m:
                silence_ends.append(float(m.group(1)))

    pairs = min(len(silence_starts), len(silence_ends))
    return [(silence_starts[i], silence_ends[i]) for i in range(pairs)]


def _segment_recursive(candidates, start, end, level_idx, levels,
                       max_segment_len, hard_split_len):
    """
    Рекурсивно подбирает точки реза для участка (start, end).

    candidates      — отсортированный список (midpoint, silence_duration) всех пауз.
    levels          — убывающий список порогов min_silence_dur, напр. [1.5, 1.15, 0.8].
    level_idx       — с какого порога начинать для этого участка.
    max_segment_len — участок длиннее этого считается «слишком длинным».
    hard_split_len  — если тишин не хватило даже на минимальном пороге, режем по этой длине.

    Логика: если участок длинный — пробуем текущий порог; нет внутренних пауз —
    понижаем порог (следующий уровень), перечитывая тот же участок. Дошли до
    минимального порога и участок всё ещё длинный — рубим жёстко по hard_split_len.
    Возвращает точки реза строго внутри (start, end).
    """
    if end - start <= max_segment_len:
        return []

    # Пробуем пороги от текущего и ниже, пока внутри участка не найдутся паузы.
    for li in range(level_idx, len(levels)):
        thr = levels[li]
        internal = [mid for (mid, dur) in candidates if start < mid < end and dur >= thr]
        if internal:
            cuts = []
            bounds = [start] + internal + [end]
            for i in range(len(bounds) - 1):
                s, e = bounds[i], bounds[i + 1]
                if i > 0:
                    cuts.append(s)  # сама граница-пауза — это точка реза
                # Каждый под-участок дочищаем уже более низким порогом.
                cuts += _segment_recursive(candidates, s, e, li + 1, levels,
                                           max_segment_len, hard_split_len)
            return sorted(set(cuts))

    # Тишин не нашлось даже на минимальном пороге — режем участок жёстко.
    # Равномерно на ceil(длина / hard_split_len) частей: все куски <= hard_split_len,
    # одинаковой длины, без крошечного хвоста в конце.
    seg_len = end - start
    n_parts = int(math.ceil(seg_len / hard_split_len))
    part = seg_len / n_parts
    return [start + part * i for i in range(1, n_parts)]


def _enforce_min_segment(cuts, total, min_seg):
    """
    Убирает точки реза, порождающие слишком короткие сегменты (< min_seg):
    такой рез отбрасывается, а огрызок сливается с соседним сегментом.
    Гарантирует, что все итоговые сегменты (включая последний) >= min_seg.
    """
    bounds = [0.0] + sorted(cuts) + [float(total)]
    kept = [0.0]
    for b in bounds[1:-1]:
        if b - kept[-1] >= min_seg:
            kept.append(b)
    # Хвост: если последний сегмент коротковат — отбрасываем последние резы.
    while len(kept) > 1 and total - kept[-1] < min_seg:
        kept.pop()
    return kept[1:]  # только внутренние точки реза


def detect_silence_timestamps(audio_path, duration, noise_tol='-30dB',
                              min_silence_dur=1.5, floor=0.8, step=0.35,
                              max_segment_len=300.0, min_segment=15.0):
    """
    Рекурсивная нарезка по паузам. Начинает с порога min_silence_dur (1.5 с) и,
    пока остаются участки длиннее max_segment_len (5 мин), понижает порог вплоть
    до floor (0.8 с). Что осталось слишком длинным — режется жёстко по 5 минут.

    Реализация: один проход silencedetect на floor (даёт все паузы >= 0.8 с с их
    длительностями), а уровни порога отрабатываются фильтрацией по длительности —
    это идентично повторному перечитыванию участка на каждом уровне.

    Parameters:
        noise_tol       — порог громкости тишины (по умолчанию -30dB).
        min_silence_dur — стартовый порог длины паузы (по умолчанию 1.5 с).
        floor           — минимальный порог длины паузы (по умолчанию 0.8 с).
        step            — шаг понижения порога (по умолчанию 0.35 с).
        max_segment_len — участок длиннее этого считается слишком длинным (5 мин).

    Returns:
        Отсортированный список точек реза (секунды).
    """
    logger.info(f"Запуск silencedetect (шум={noise_tol}, мин. пауза={floor}s, "
                f"рекурсия {min_silence_dur}→{floor}s, макс. участок={max_segment_len}s)...")

    raw = _run_silencedetect(audio_path, noise_tol, floor)

    # Кандидаты: середины пауз с их длительностью. Стартовую тишину пропускаем.
    candidates = []
    for s_start, s_end in raw:
        if s_start <= 0.1:
            continue
        midpoint = (s_start + s_end) / 2
        if 0 < midpoint < duration - 1.0:  # не вплотную к концу
            candidates.append((midpoint, s_end - s_start))
    candidates.sort()

    # Убывающие уровни порога: 1.5, 1.15, ..., 0.8.
    levels = []
    t = min_silence_dur
    while t > floor:
        levels.append(round(t, 3))
        t -= step
    levels.append(floor)

    cuts = _segment_recursive(candidates, 0.0, float(duration), 0, levels,
                              max_segment_len, hard_split_len=max_segment_len)
    cuts = sorted(c for c in cuts if 0 < c < duration)
    cuts = _enforce_min_segment(cuts, duration, min_segment)

    logger.info(f"silencedetect: пауз-кандидатов — {len(candidates)}, "
                f"уровни порога — {levels}, итоговых точек разреза — {len(cuts)}")
    return cuts


def download_audio(url):
    """
    Download audio from YouTube video using yt-dlp.
    Returns path to downloaded MP3 file, title, description, duration, chapters, temp_dir_path.
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
        'js_runtimes': {'node': {}}, # использовать установленный Node.js для расшифровки ссылок YouTube (иначе троттлинг и таймауты)
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info['title']
        description = info.get('description', '')
        duration = info['duration']
        chapters = info.get('chapters')  # структурные главы YouTube, если заданы автором
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
    
    return mp3_file, title, description, duration, chapters, temp_dir_path

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
        audio_path, title, description, duration, chapters, temp_dir_path = download_audio(url)
        print(f"Video title: {title}")
        print(f"Video duration: {duration}s")

        # Приоритет источников таймкодов:
        #   1) заданные вручную;
        #   2) структурные главы YouTube (info['chapters']) — самый надёжный источник;
        #   3) регэкспы по описанию;
        #   4) скрапинг HTML-страницы (слабый фолбэк);
        #   5) детект пауз (ниже, если ничего не нашли).
        if manual_timestamps:
            timestamps = [int(t) for t in manual_timestamps.split(',') if t.strip().isdigit()]
            print(f"Using manual timestamps: {timestamps}")
        elif chapters:
            # Границы треков — это старты глав (стартовую главу с 0 пропускаем).
            timestamps = sorted({round(float(c['start_time']), 3) for c in chapters
                                 if c.get('start_time') and float(c['start_time']) > 0})
            print(f"Using {len(chapters)} chapters from metadata: {timestamps}")
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
