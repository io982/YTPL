import yt_dlp
import re
import os
import sys
import math
import argparse
import subprocess
import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup
import tempfile
import logging

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ways of finding cut points.
#   MODE_SILENCE - silence analysis. The default: on mixes, author timestamps
#                  often mark 15-20 minute blocks rather than tracks, so cutting
#                  by them glues several tracks into a single file.
#   MODE_META    - video chapters/description. Only when explicitly requested.
#   MODE_MANUAL  - timestamps supplied by the user.
MODE_SILENCE = 'silence'
MODE_META = 'meta'
MODE_MANUAL = 'manual'

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

def timestamps_from_metadata(url, description, chapters):
    """
    Timestamps taken from the video metadata (used in MODE_META only).
    Source order:
      1) structural YouTube chapters (info['chapters']) - the most reliable;
      2) regexes over the description;
      3) scraping the page HTML (weak fallback).

    Returns:
        (list of timestamps in seconds, name of the source).
    """
    if chapters:
        # Track boundaries are the chapter starts (the one at 0 is skipped).
        timestamps = sorted({round(float(c['start_time']), 3) for c in chapters
                             if c.get('start_time') and float(c['start_time']) > 0})
        if timestamps:
            return timestamps, f'YouTube chapters ({len(chapters)} of them)'

    timestamps = extract_timestamps(description)
    if timestamps:
        return timestamps, 'video description'

    timestamps = extract_timestamps(get_full_page_text(url))
    if timestamps:
        return timestamps, 'video page HTML'

    return [], 'nothing found'


def _run_silencedetect(audio_path, noise_tol, min_silence_dur):
    """
    Run ffmpeg silencedetect once and return the detected pauses as
    (start, end) tuples in seconds.
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
    Recursively pick cut points for the (start, end) piece.

    candidates      - sorted list of (midpoint, silence_duration) for every pause.
    levels          - descending list of min_silence_dur thresholds, e.g. [1.5, 1.15, 0.8].
    level_idx       - which threshold to start from for this piece.
    max_segment_len - a piece longer than this counts as "too long".
    hard_split_len  - if even the lowest threshold found no pauses, split by this length.

    The logic: while the piece is long, try the current threshold; if it holds no
    internal pauses, lower the threshold (next level) and look at the same piece
    again. Once the lowest threshold is reached and the piece is still long, split
    it hard by hard_split_len. Returns cut points strictly inside (start, end).
    """
    if end - start <= max_segment_len:
        return []

    # Try thresholds from the current one downwards until pauses show up inside.
    for li in range(level_idx, len(levels)):
        thr = levels[li]
        internal = [mid for (mid, dur) in candidates if start < mid < end and dur >= thr]
        if internal:
            cuts = []
            bounds = [start] + internal + [end]
            for i in range(len(bounds) - 1):
                s, e = bounds[i], bounds[i + 1]
                if i > 0:
                    cuts.append(s)  # the pause boundary itself is a cut point
                # Every sub-piece is refined with an already lower threshold.
                cuts += _segment_recursive(candidates, s, e, li + 1, levels,
                                           max_segment_len, hard_split_len)
            return sorted(set(cuts))

    # No pauses even at the lowest threshold - split the piece hard.
    # Evenly into ceil(length / hard_split_len) parts: every part is
    # <= hard_split_len, all of equal length, with no tiny tail at the end.
    seg_len = end - start
    n_parts = int(math.ceil(seg_len / hard_split_len))
    part = seg_len / n_parts
    return [start + part * i for i in range(1, n_parts)]


def _enforce_min_segment(cuts, total, min_seg):
    """
    Drop cut points that would produce segments shorter than min_seg: such a cut
    is discarded and the stub is merged into the neighbouring segment.
    Guarantees every resulting segment (the last one included) is >= min_seg.
    """
    bounds = [0.0] + sorted(cuts) + [float(total)]
    kept = [0.0]
    for b in bounds[1:-1]:
        if b - kept[-1] >= min_seg:
            kept.append(b)
    # Tail: while the last segment is too short, drop the trailing cuts.
    while len(kept) > 1 and total - kept[-1] < min_seg:
        kept.pop()
    return kept[1:]  # internal cut points only


def detect_silence_timestamps(audio_path, duration, noise_tol='-30dB',
                              min_silence_dur=1.5, floor=0.8, step=0.35,
                              max_segment_len=300.0, min_segment=15.0):
    """
    Recursive splitting by pauses. Starts at the min_silence_dur threshold
    (1.5 s) and keeps lowering it down to floor (0.8 s) while any piece is
    longer than max_segment_len (5 min). Whatever stays too long is split hard
    by that same length.

    Implementation: a single silencedetect pass at floor (which yields every
    pause >= 0.8 s together with its duration), and the threshold levels are
    then applied by filtering on duration - identical to re-reading the piece
    at every level.

    Parameters:
        noise_tol       - silence volume threshold (default -30dB).
        min_silence_dur - starting pause-length threshold (default 1.5 s).
        floor           - lowest pause-length threshold (default 0.8 s).
        step            - threshold lowering step (default 0.35 s).
        max_segment_len - a piece longer than this counts as too long (5 min).
        min_segment     - minimum track length (default 15 s).

    Returns:
        Sorted list of cut points (seconds).
    """
    logger.info(f"Running silencedetect (noise={noise_tol}, min pause={floor}s, "
                f"recursion {min_silence_dur}->{floor}s, max piece={max_segment_len}s)...")

    raw = _run_silencedetect(audio_path, noise_tol, floor)

    # Candidates: pause midpoints with their durations. Leading silence is skipped.
    candidates = []
    for s_start, s_end in raw:
        if s_start <= 0.1:
            continue
        midpoint = (s_start + s_end) / 2
        if 0 < midpoint < duration - 1.0:  # not right up against the end
            candidates.append((midpoint, s_end - s_start))
    candidates.sort()

    # Descending threshold levels: 1.5, 1.15, ..., 0.8.
    levels = []
    t = min_silence_dur
    while t > floor:
        levels.append(round(t, 3))
        t -= step
    levels.append(floor)

    # The first pass at the starting threshold always runs, whatever the piece
    # length: a clear pause between tracks is a track boundary in a short video
    # too (otherwise an album shorter than max_segment_len would never be split).
    base = sorted(mid for (mid, dur) in candidates if dur >= levels[0])
    cuts = list(base)

    # Threshold lowering and hard splitting apply only to the pieces that are
    # still too long after the first pass.
    bounds = [0.0] + base + [float(duration)]
    for i in range(len(bounds) - 1):
        cuts += _segment_recursive(candidates, bounds[i], bounds[i + 1], 1, levels,
                                   max_segment_len, hard_split_len=max_segment_len)

    cuts = sorted({c for c in cuts if 0 < c < duration})
    cuts = _enforce_min_segment(cuts, duration, min_segment)

    logger.info(f"silencedetect: pause candidates - {len(candidates)}, "
                f"threshold levels - {levels}, resulting cut points - {len(cuts)}")
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
        'socket_timeout': 60,       # raised socket timeout (the default was 20)
        'retries': 10,              # download retry attempts
        'fragment_retries': 10,     # retries for fragmented streams
        'http_chunk_size': 10485760, # 10 MB chunks - help on an unstable connection
        'js_runtimes': {'node': {}}, # use the installed Node.js to decrypt YouTube URLs (otherwise throttling and timeouts)
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info['title']
        description = info.get('description', '')
        duration = info['duration']
        chapters = info.get('chapters')  # structural YouTube chapters, if the author set them
        ext = info.get('ext', 'm4a')

        # Locate the downloaded file
        downloaded_file = None
        for f in os.listdir(temp_dir_path):
            downloaded_file = os.path.join(temp_dir_path, f)
            break

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise FileNotFoundError("Audio file was not downloaded successfully")

        if ext.lower() == 'mp3':
            mp3_file = downloaded_file  # already MP3, use as is
        else:
            mp3_file = os.path.join(temp_dir_path, 'audio.mp3')
            cmd = [ffmpeg_path, '-i', downloaded_file, '-q:a', '0', '-y', mp3_file]
            subprocess.run(cmd, check=True)
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)

    return mp3_file, title, description, duration, chapters, temp_dir_path

def split_audio(audio_path, timestamps, duration, base_name='video', output_dir='.'):
    """
    Split audio into segments based on timestamps using ffmpeg.
    Returns list of created segment paths.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    segments = []
    created_files = []
    start = 0

    # Filter and validate the timestamps
    valid_timestamps = []
    for ts in timestamps:
        if ts <= start:
            logger.warning(f"Skipped invalid timestamp: {ts}s (must be > {start}s)")
            continue
        if ts > duration:
            logger.warning(f"Skipped timestamp beyond the video: {ts}s (duration: {duration}s)")
            continue
        valid_timestamps.append(ts)

    timestamps = valid_timestamps

    for i, end in enumerate(timestamps):
        if end > start:
            segments.append((start, end))
            start = end
        else:
            logger.warning(f"Skipped overlapping segment: {start}s - {end}s")

    # Add the last segment, up to the end of the video
    if start < duration:
        segments.append((start, duration))
    elif start == duration and segments:
        logger.info("The last timestamp matches the end of the video, no final segment needed")
    elif not segments:
        logger.warning("No valid segments to create")

    logger.info(f"Creating {len(segments)} segments")

    for i, (start_time, end_time) in enumerate(segments):
        segment_num = f"{i+1:04d}"
        output_filename = os.path.join(output_dir, f"{base_name}_{segment_num}.mp3")
        duration_seg = end_time - start_time

        logger.info(f"Segment {i+1}/{len(segments)}: {start_time}s - {end_time}s ({duration_seg:.1f}s)")

        cmd = [ffmpeg_path, '-i', audio_path, '-ss', str(start_time), '-t', str(duration_seg), '-c', 'copy', output_filename]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Failed to create segment {i+1}: {result.stderr}")
            continue

        if os.path.exists(output_filename):
            created_files.append(output_filename)
            logger.info(f"Created: {output_filename}")
        else:
            logger.error(f"Segment file was not created: {output_filename}")

    return created_files

def process_youtube_video(url, manual_timestamps=None, mode=MODE_SILENCE,
                          silence_opts=None):
    """
    Main function to process YouTube video.

    Parameters:
        manual_timestamps - a string like '145,325'; when given, the mode is always MODE_MANUAL.
        mode              - MODE_SILENCE (the default), MODE_META or MODE_MANUAL.
        silence_opts      - dict of detect_silence_timestamps parameters.

    Returns list of created segment paths.
    """
    temp_dir_path = None  # holds the path to the temporary directory

    try:
        # Download audio (title, description and duration come with the file)
        logger.info("Downloading audio...")
        audio_path, title, description, duration, chapters, temp_dir_path = download_audio(url)
        print(f"Video title: {title}")
        print(f"Video duration: {duration}s")

        # The source of cut points is decided by the mode, not by whether the
        # author supplied timestamps: chapters and description are NOT given
        # priority, because on mixes they usually mark blocks of several tracks.
        # See MODE_* above.
        if manual_timestamps:
            mode = MODE_MANUAL

        if mode == MODE_MANUAL:
            timestamps = [int(t) for t in str(manual_timestamps).split(',')
                          if t.strip().isdigit()]
            logger.info(f"Mode: manual timestamps - {timestamps}")
        elif mode == MODE_META:
            logger.info("Mode: timestamps from metadata (explicitly requested).")
            timestamps, source = timestamps_from_metadata(url, description, chapters)
            logger.info(f"Source: {source}; cut points - {len(timestamps)}: {timestamps}")
            if not timestamps:
                logger.warning("No timestamps in the metadata - falling back to silence analysis.")
                timestamps = detect_silence_timestamps(audio_path, duration,
                                                       **(silence_opts or {}))
        else:
            logger.info("Mode: silence analysis (metadata ignored).")
            timestamps = detect_silence_timestamps(audio_path, duration,
                                                   **(silence_opts or {}))

        if not timestamps:
            logger.warning("No cut points found - the audio will stay a single file.")
            if sys.stdin is not None and sys.stdin.isatty():
                cont = input("Save as a single file? (y/n): ").strip().lower()
                if cont not in ('', 'y', 'yes'):
                    return []

        # Split audio
        base_name = re.sub(r'[^\w\-_\. ]', '_', title).replace(' ', '_')
        output_dir = base_name
        os.makedirs(output_dir, exist_ok=True)

        logger.info("Splitting audio into segments...")
        created_segments = split_audio(audio_path, timestamps, duration, base_name, output_dir)

        print(f"\n{'='*50}")
        print(f"Processing finished successfully!")
        print(f"Segments created: {len(created_segments)}")
        print(f"Output folder: {output_dir}/")
        print(f"{'='*50}")

        return created_segments

    except Exception as e:
        logger.error(f"Error: {e}")
        raise
    finally:
        # Clean up the temporary files
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                import shutil
                shutil.rmtree(temp_dir_path)
                logger.info(f"Temporary files removed: {temp_dir_path}")
            except Exception as cleanup_error:
                logger.warning(f"Could not remove the temporary files: {cleanup_error}")

def build_parser():
    """
    Command-line argument parsing. With no arguments the script asks for
    everything itself.
    """
    p = argparse.ArgumentParser(
        prog='YTPL',
        description='Downloads audio from YouTube and splits it into separate '
                    'tracks. Splits by pauses in the audio by default; chapters '
                    'and description timestamps are only used with '
                    '--from-description.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('url', nargs='?', help='YouTube video URL')
    p.add_argument('-t', '--timestamps', metavar='SEC',
                   help="your own timestamps in seconds, comma-separated, e.g. '145,325'")
    p.add_argument('-d', '--from-description', '--desc', '--chapters',
                   dest='from_description', action='store_true',
                   help='force timestamps from the video chapters/description '
                        'instead of silence analysis')
    p.add_argument('--silence', dest='force_silence', action='store_true',
                   help='explicitly select silence analysis (the default mode)')

    g = p.add_argument_group('silence analysis settings')
    g.add_argument('--noise', default='-30dB', metavar='dB',
                   help='volume threshold below which sound counts as silence; '
                        'the minus sign may be omitted: --noise 30dB or --noise=-30dB')
    g.add_argument('--min-silence', type=float, default=1.5, metavar='SEC',
                   help='starting pause-length threshold')
    g.add_argument('--floor', type=float, default=0.8, metavar='SEC',
                   help='lowest pause-length threshold when lowering')
    g.add_argument('--max-segment', type=float, default=300.0, metavar='SEC',
                   help='a piece longer than this is re-split with a lower threshold')
    g.add_argument('--min-segment', type=float, default=15.0, metavar='SEC',
                   help='minimum track length: short stubs are merged into the neighbour')
    return p


def prompt_mode():
    """
    Interactive mode selection. Enter - silence analysis (the default mode).
    Returns (mode, manual_timestamps).
    """
    print()
    print("Splitting mode:")
    print("  [Enter] - by pauses in the audio (default: works for mixes and albums)")
    print("  d       - by the video's chapters/description timestamps")
    print("  145,325 - your own timestamps in seconds, comma-separated")
    choice = input("Choice: ").strip()
    if not choice:
        return MODE_SILENCE, None
    if choice.lower() in ('d', 'desc', 'description', 'chapters'):
        return MODE_META, None
    return MODE_MANUAL, choice


def resolve_mode(args, interactive=None):
    """
    Work out the splitting mode from the command-line arguments.

    Priority: own timestamps -> forced metadata -> the default mode. When a URL
    is passed as an argument and no flags are given, run quietly in the default
    mode; with no arguments at all, ask the user for the mode.

    Parameters:
        interactive - overrides the automatic detection of an interactive run
                      (by default: whether stdin is attached to a terminal).

    Returns:
        (mode, manual_timestamps).
    """
    if args.timestamps:
        return MODE_MANUAL, args.timestamps
    if args.from_description:
        return MODE_META, None
    if args.force_silence or args.url:
        return MODE_SILENCE, None
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        return prompt_mode()
    return MODE_SILENCE, None


def normalize_noise(value):
    """
    Bring the volume threshold into the form ffmpeg understands ('-30dB').

    argparse rejects '--noise -30dB': a value starting with a minus is read as
    another flag. So the form without the minus ('--noise 30dB') is accepted too
    and turned into a negative value.
    """
    value = str(value).strip()
    if not value:
        return '-30dB'
    if not value.startswith('-'):
        value = '-' + value.lstrip('+')
    return value


def silence_opts_from_args(args):
    """
    Collect the silence analysis parameters from the command-line arguments.
    """
    return {
        'noise_tol': normalize_noise(args.noise),
        'min_silence_dur': args.min_silence,
        'floor': args.floor,
        'max_segment_len': args.max_segment,
        'min_segment': args.min_segment,
    }


if __name__ == "__main__":
    args = build_parser().parse_args()

    # Check that ffmpeg is available
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        if not os.path.exists(ffmpeg_path):
            raise RuntimeError(f"FFmpeg not found at: {ffmpeg_path}")
        logger.info(f"FFmpeg found: {ffmpeg_path}")
    except Exception as e:
        logger.error(f"FFmpeg not found. Install imageio-ffmpeg: pip install imageio-ffmpeg")
        exit(1)

    url = args.url or input("Enter YouTube video URL: ").strip()
    if not url:
        logger.error("No URL given.")
        exit(1)

    # Mode: flags come first, then the interactive question. When a URL is passed
    # as an argument and no flags are given, run quietly in the default mode.
    mode, manual_timestamps = resolve_mode(args)
    silence_opts = silence_opts_from_args(args)

    try:
        process_youtube_video(url, manual_timestamps, mode=mode,
                              silence_opts=silence_opts)
    except KeyboardInterrupt:
        logger.info("\nOperation cancelled by the user")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        print(f"\n[ERROR] {e}")
        print("Possible fixes:")
        print("  1. Check your internet connection")
        print("  2. Update yt-dlp: pip install -U yt-dlp")
        print("  3. Update imageio-ffmpeg: pip install -U imageio-ffmpeg")
        print("  4. Try a different YouTube link")
