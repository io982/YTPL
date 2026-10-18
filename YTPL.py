import yt_dlp
import re
import os
import sys
import math
import argparse
import platform
import shutil
import subprocess
import urllib.parse
import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup
import tempfile
import time
import logging

# Greeting banner. The art is a raw string: ten of its lines end with a
# backslash, which a plain literal would read as an escape.
BANNER = r"""
      _____                _____                    _____                    _____
     |\    \              /\    \                  /\    \                  /\    \
     |:\____\            /::\    \                /::\    \                /::\____\
     |::|   |            \:::\    \              /::::\    \              /:::/    /
     |::|   |             \:::\    \            /::::::\    \            /:::/    /
     |::|   |              \:::\    \          /:::/\:::\    \          /:::/    /
     |::|   |               \:::\    \        /:::/__\:::\    \        /:::/    /
     |::|   |               /::::\    \      /::::\   \:::\    \      /:::/    /
     |::|___|______        /::::::\    \    /::::::\   \:::\    \    /:::/    /
     /::::::::\    \      /:::/\:::\    \  /:::/\:::\   \:::\____\  /:::/    /
    /::::::::::\____\    /:::/  \:::\____\/:::/  \:::\   \:::|    |/:::/____/
   /:::/~~~~/~~         /:::/    \::/    /\::/    \:::\  /:::|____|\:::\    \
  /:::/    /           /:::/    / \/____/  \/_____/\:::\/:::/    /  \:::\    \
 /:::/    /           /:::/    /                    \::::::/    /    \:::\    \
/:::/    /           /:::/    /                      \::::/    /      \:::\    \
\::/    /            \::/    /                        \::/____/        \:::\    \
 \/____/              \/____/                          ~~               \:::\    \
                                                                         \:::\    \
                                                                          \:::\____\
                                                                           \::/    /
                                                                            \/____/
"""
BANNER_WIDTH = 84

# Logging setup. The default log file carries the start of the run, so every
# session gets its own file instead of appending to a shared one: a log you
# want to read or send is then exactly one run. strftime placeholders are
# expanded, so --log-file takes a pattern of its own just as well.
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
DEFAULT_LOG_FILE = os.path.join('logs', 'ytpl-%Y%m%d-%H%M%S.log')

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# Whether to ask yt-dlp for its own debug output; set by setup_logging().
_ytdlp_verbose = False

# Remote components yt-dlp may fetch; set from --remote-components.
_remote_components = []

# ffmpeg and node speak UTF-8. Decoding them with the system code page
# instead - which is what text=True does - raises on the first byte it cannot
# map: a track with a Korean title made ffmpeg print one, and the exception
# landed in subprocess's reader thread, taking that output with it. Losing
# ffmpeg's output is not cosmetic: silencedetect's findings are parsed out of
# it, and without them a track is silently left unsplit.
SUBPROCESS_TEXT = {'capture_output': True, 'text': True,
                   'encoding': 'utf-8', 'errors': 'replace'}

# What a second of audio costs on disk once converted. ffmpeg is asked for
# '-q:a 0', which measured 31857 bytes a second over the first dozen tracks of
# a real playlist - about 109 MB per hour of audio.
BYTES_PER_SECOND = 32000

# A run stops rather than filling the disk to the last byte.
DEFAULT_MIN_FREE_MB = 500

# Every download works in its own folder under the system temp directory.
TEMP_PREFIX = 'ytpl_audio_'

# How quiet a passage has to be to count as a gap between tracks. At -30dB a
# soft musical passage qualifies: one 9-minute track had seven "pauses" at
# -30dB and none at all at -40dB, and was being cut into three. A real gap
# between tracks is far quieter than the quietest music.
DEFAULT_NOISE = '-40dB'

# A piece longer than this is re-read at a lower pause threshold and, if that
# finds nothing either, cut where no pause was. That last resort is for a mix
# recorded as one unbroken stretch; a song is not that, and at the old 5 minutes
# a 5:28 track with no internal pause at all was being cut in half for nothing.
# 15 minutes is longer than songs get and still breaks up a long mix.
DEFAULT_MAX_SEGMENT = 900.0

# A file by this name asks a playlist run to stop at the next track boundary.
STOP_FILE = 'STOP'

# Consecutive track failures that end a playlist run. Downloads that stop
# working stay broken - an expired token, a dead network, a console window
# closed from under the subprocesses - and grinding through several hundred
# tracks to fail each one helps nobody.
DEFAULT_MAX_FAILURES = 5

# Attempts per track. YouTube hands out a different player client each
# extraction, and some of them answer 403 on the media URL, so a second go
# at the same track often just works.
DEFAULT_TRACK_ATTEMPTS = 3

# Seconds between those attempts.
TRACK_RETRY_WAIT = 3

# The 403 hint is worth saying once a run, not once a track.
_403_hint_shown = False

# A key press waiting to be acted on at the next track boundary.
_pending_control = None

# Cached result of node_is_usable(), so the probe runs once per process.
_node_checked = None

# How long to wait for 'node --version' before giving up on the runtime.
NODE_PROBE_TIMEOUT = 10

# A 403 on the media URL almost always means YouTube's JS challenge went
# unsolved, which needs both Node.js and the solver script.
HTTP_403_HINT = (
    "A 403 here almost always means YouTube's JS challenge was not solved. "
    "Install the solver with 'pip install yt-dlp-ejs' (Node.js is required too), "
    "or let yt-dlp fetch one with --remote-components ejs:github."
)

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

def setup_logging(log_file=DEFAULT_LOG_FILE, verbose=False):
    """
    Console logging plus a full-detail log file.

    The console is left with warnings and errors only (everything, with
    verbose) while the file always records DEBUG. That is deliberate: progress
    is shown by the status line, and what tells a YouTube-side block apart from
    a local problem - yt-dlp's own output, the environment banner, tracebacks -
    is far too noisy to put in front of someone watching a playlist download.

    Returns the path of the log file, or None when no file is written.
    """
    global _ytdlp_verbose
    _ytdlp_verbose = verbose

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        # The console carries the progress line and the per-track results; the
        # running commentary would only bury them, so it goes to the file.
        handler.setLevel(logging.DEBUG if verbose else logging.WARNING)

    if not log_file:
        return None

    # 'logs/ytpl-%Y%m%d-%H%M%S.log' -> 'logs/ytpl-20260118-174500.log'
    log_file = time.strftime(log_file)
    try:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
    except OSError as e:
        logger.warning(f"Could not open the log file {log_file}: {e}")
        return None

    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)
    return log_file


def format_size(num_bytes):
    """
    Bytes as something readable: 512 KB, 7.9 MB, 3.4 GB.
    """
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(size) < 1024 or unit == 'GB':
            return f"{int(size)} B" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024


def estimate_download_size(entries):
    """
    Roughly what a playlist will take on disk, from the durations in its
    listing. Entries whose duration YouTube did not give are charged the
    average of those it did, so a few unknowns do not flatter the estimate.

    Returns bytes, or 0 when no duration is known at all.
    """
    known = [e['duration'] for e in entries if e.get('duration')]
    if not known:
        return 0
    average = sum(known) / len(known)
    total = sum(e.get('duration') or average for e in entries)
    return int(total * BYTES_PER_SECOND)


def free_space(path):
    """
    Free bytes on the volume holding path, or None when that cannot be read.
    The path itself need not exist yet - its nearest existing parent is used.
    """
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError as e:
        logger.debug(f"Could not read the free space on {probe}: {e}")
        return None


def clean_stale_temp_dirs(older_than_minutes=10):
    """
    Remove temp folders left behind by earlier runs.

    Each download works in its own folder and removes it when it is done, but
    a run that is killed never gets that far, and a few of those leave
    hundreds of megabytes in the temp directory.

    Folders younger than the cutoff are left alone in case another run is
    working in one; on Windows a folder in use cannot be deleted anyway, and
    the attempt is made quietly.

    Returns the number of folders removed.
    """
    cutoff = time.time() - older_than_minutes * 60
    root = tempfile.gettempdir()
    removed = 0

    try:
        names = os.listdir(root)
    except OSError as e:
        logger.debug(f"Could not list the temp directory: {e}")
        return 0

    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue

        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            logger.debug(f"Temp folder {name} is still in use, left alone")
        else:
            removed += 1
            logger.debug(f"Removed stale temp folder {name}")

    if removed:
        logger.info(f"Removed {removed} temp folder(s) left by earlier runs")
    return removed


def warn_403_once(error):
    """
    Say the 403 hint on the console the first time a track is given up on for
    one, and only then: a 403 that a retry gets past needs no advice, and 400
    of them need it once.
    """
    global _403_hint_shown
    if '403' not in str(error):
        return
    if _403_hint_shown:
        logger.debug(HTTP_403_HINT)
        return
    logger.error(HTTP_403_HINT)
    _403_hint_shown = True


def node_is_usable(timeout=NODE_PROBE_TIMEOUT):
    """
    Whether Node.js answers 'node --version' within the timeout.

    yt-dlp probes the JavaScript runtime the same way but without a deadline,
    so a node process that wedges on startup takes the whole run down with it:
    it sits there with a single thread and no CPU while the download waits
    forever. Checking with a timeout means such a run carries on (and fails
    fast) instead of hanging.
    """
    try:
        result = subprocess.run(['node', '--version'], timeout=timeout,
                                **SUBPROCESS_TEXT)
    except subprocess.TimeoutExpired:
        logger.warning(f"Node.js did not answer within {timeout}s - continuing "
                       "without a JavaScript runtime (downloads may fail with 403)")
        return False
    except OSError as e:
        logger.warning(f"Node.js is not available ({e}) - continuing without a "
                       "JavaScript runtime (downloads may fail with 403)")
        return False

    if result.returncode != 0:
        logger.warning(f"'node --version' failed with code {result.returncode} - "
                       "continuing without a JavaScript runtime")
        return False

    logger.debug(f"Node.js {result.stdout.strip()} answered the probe")
    return True


def print_banner():
    """
    Greeting. The art needs BANNER_WIDTH columns, so a narrower terminal gets a
    plain title line instead of a wrapped, unreadable banner.
    """
    try:
        width = shutil.get_terminal_size().columns
    except Exception:
        width = 80
    if width >= BANNER_WIDTH:
        print(BANNER)
    else:
        print('YTPL - YouTube To PlayList converter')
        print()


def log_environment():
    """
    Record what this run is working with. A download that fails for every video
    (YouTube blocking an outdated yt-dlp, say) looks nothing like a local
    problem, and these lines are what tell them apart after the fact.
    """
    logger.info(f"YTPL starting - Python {sys.version.split()[0]}, {platform.platform()}")
    logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")
    try:
        logger.info(f"ffmpeg {imageio_ffmpeg.get_ffmpeg_version()} "
                    f"at {imageio_ffmpeg.get_ffmpeg_exe()}")
    except Exception as e:
        logger.warning(f"Could not query ffmpeg: {e}")
    try:
        import yt_dlp_ejs
        logger.info("yt-dlp-ejs (JS challenge solver): "
                    f"{getattr(yt_dlp_ejs, '__version__', 'installed')}")
    except ImportError:
        logger.warning("yt-dlp-ejs is not installed. " + HTTP_403_HINT)

    logger.debug(f"Working directory: {os.getcwd()}")
    logger.debug(f"Command line: {' '.join(sys.argv)}")


class _YtdlpLogger:
    """
    Routes yt-dlp's own messages into the logging module, so they reach the log
    file. This is where a '403 Forbidden', a missing JavaScript runtime or a
    format-selection problem actually shows up.

    None of it reaches the console on its own: YTPL reports the outcome of each
    track itself, and yt-dlp's running commentary would bury it. Everything is
    in the log file, and verbose puts it back on screen.
    """
    def debug(self, msg):
        # yt-dlp sends ordinary screen output here too; only the lines it marks
        # itself are real debug detail.
        if msg.startswith('[debug] '):
            logger.debug(msg)
        else:
            logger.info(msg)

    def info(self, msg):
        logger.info(msg)

    def warning(self, msg):
        logger.info(f"[yt-dlp warning] {msg}")

    def error(self, msg):
        # YTPL logs the failure itself, with the same text, right after.
        logger.info(f"[yt-dlp error] {msg}")


def _download_progress(status):
    """
    One line per finished download instead of yt-dlp's live progress bar, which
    would be thousands of lines in the log file on a long playlist.
    """
    # Keys are read here too, so a press during a long download is answered
    # straight away instead of in silence until the track ends.
    poll_keys()

    state = status.get('status')
    if state == 'finished':
        size = status.get('total_bytes') or status.get('downloaded_bytes') or 0
        logger.info(f"Downloaded {size / 1048576:.1f} MB "
                    f"in {status.get('elapsed') or 0:.0f}s")
    elif state == 'error':
        logger.error(f"yt-dlp reported a download error: {status.get('filename')}")


def sanitize_name(name):
    """
    Turn a video or playlist title into a safe file/folder name.
    """
    return re.sub(r'[^\w\-_\. ]', '_', name).replace(' ', '_')


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
    result = subprocess.run(cmd, **SUBPROCESS_TEXT)

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


def detect_silence_timestamps(audio_path, duration, noise_tol=DEFAULT_NOISE,
                              min_silence_dur=1.5, floor=0.8, step=0.35,
                              max_segment_len=DEFAULT_MAX_SEGMENT, min_segment=15.0):
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
        noise_tol       - how quiet counts as silence (default -40dB).
        min_silence_dur - starting pause-length threshold (default 1.5 s).
        floor           - lowest pause-length threshold (default 0.8 s).
        step            - threshold lowering step (default 0.35 s).
        max_segment_len - a piece longer than this counts as too long, and is
                          re-read at a lower threshold and, failing that, cut
                          where nothing says to. 0 turns that off: only real
                          pauses are ever cut at.
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
    # still too long after the first pass. With max_segment_len at 0 there is
    # no such thing as too long, and nothing is cut where no pause was found.
    if max_segment_len:
        bounds = [0.0] + base + [float(duration)]
        for i in range(len(bounds) - 1):
            cuts += _segment_recursive(candidates, bounds[i], bounds[i + 1], 1, levels,
                                       max_segment_len, hard_split_len=max_segment_len)

    cuts = sorted({c for c in cuts if 0 < c < duration})
    cuts = _enforce_min_segment(cuts, duration, min_segment)

    logger.info(f"silencedetect: pause candidates - {len(candidates)}, "
                f"threshold levels - {levels}, resulting cut points - {len(cuts)}")
    return cuts


def download_audio(url, verbose=None, remote_components=None):
    """
    Download audio from YouTube video using yt-dlp.
    Returns path to downloaded MP3 file, title, description, duration, chapters, temp_dir_path.

    Parameters:
        verbose           - ask yt-dlp for its own debug output; None uses what
                            setup_logging() was given.
        remote_components - components yt-dlp may fetch when it needs one;
                            None uses what --remote-components was given.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    temp_dir_path = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    outtmpl = os.path.join(temp_dir_path, 'audio.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True,              # yt-dlp's own output goes through the logger below
        'no_warnings': False,
        'noprogress': True,         # the progress bar would flood the log file
        'logger': _YtdlpLogger(),
        'progress_hooks': [_download_progress],
        'verbose': _ytdlp_verbose if verbose is None else verbose,
        'socket_timeout': 60,       # raised socket timeout (the default was 20)
        'retries': 10,              # download retry attempts
        'fragment_retries': 10,     # retries for fragmented streams
        'http_chunk_size': 10485760, # 10 MB chunks - help on an unstable connection
        'noplaylist': True,         # a '&list=' URL means this video only; playlists are walked by process_playlist
    }

    # Node decrypts YouTube's URLs; without it formats go missing and the media
    # URLs come back 403. Only offered to yt-dlp once it has answered a probe,
    # because a wedged node would otherwise block the download indefinitely.
    global _node_checked
    if _node_checked is None:
        _node_checked = node_is_usable()
    if _node_checked:
        ydl_opts['js_runtimes'] = {'node': {}}

    components = _remote_components if remote_components is None else remote_components
    if components:
        ydl_opts['remote_components'] = list(components)
        logger.debug(f"Remote components allowed: {components}")
    logger.debug(f"Downloading {url} into {temp_dir_path}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        # Logged, not printed: this may be one attempt of several, and the
        # caller reports what became of the track.
        logger.info(f"Download failed for {url}: {e}")
        logger.debug("Download traceback", exc_info=True)
        # The runtime may be what broke (a closed console window takes the
        # subprocesses with it), so probe it again rather than trusting the
        # earlier answer for the rest of the run.
        _node_checked = None
        # The caller never received the path, so it cannot clean this up.
        shutil.rmtree(temp_dir_path, ignore_errors=True)
        raise

    title = info['title']
    description = info.get('description', '')
    duration = info['duration']
    chapters = info.get('chapters')  # structural YouTube chapters, if the author set them
    ext = info.get('ext', 'm4a')

    logger.debug(f"Resolved: title={title!r}, duration={duration}s, "
                 f"format={info.get('format_id')} {ext} abr={info.get('abr')}, "
                 f"chapters={len(chapters) if chapters else 0}, "
                 f"description={len(description)} chars")

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
        logger.debug(f"Converting {ext} to mp3")
        mp3_file = os.path.join(temp_dir_path, 'audio.mp3')
        cmd = [ffmpeg_path, '-i', downloaded_file, '-q:a', '0', '-y', mp3_file]
        # Captured, or ffmpeg's progress meter lands on top of the console's
        # own status line.
        result = subprocess.run(cmd, **SUBPROCESS_TEXT)
        if result.returncode != 0:
            logger.debug(f"ffmpeg said:{chr(10)}{result.stderr}")
            raise RuntimeError(f"ffmpeg could not convert {ext} to mp3 "
                               f"(exit code {result.returncode})")
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
        # A track that was not split is just itself; the segment number would
        # only be noise on it.
        suffix = f"_{i + 1:04d}" if len(segments) > 1 else ""
        output_filename = os.path.join(output_dir, f"{base_name}{suffix}.mp3")
        duration_seg = end_time - start_time

        logger.info(f"Segment {i+1}/{len(segments)}: {start_time}s - {end_time}s ({duration_seg:.1f}s)")

        cmd = [ffmpeg_path, '-i', audio_path, '-ss', str(start_time), '-t', str(duration_seg), '-c', 'copy', output_filename]
        result = subprocess.run(cmd, **SUBPROCESS_TEXT)

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
                          silence_opts=None, output_dir=None, base_name=None):
    """
    Main function to process YouTube video.

    Parameters:
        manual_timestamps - a string like '145,325'; when given, the mode is always MODE_MANUAL.
        mode              - MODE_SILENCE (the default), MODE_META or MODE_MANUAL.
        silence_opts      - dict of detect_silence_timestamps parameters.
        output_dir        - folder to write the segments into. The default is
                            a folder named after the video, next to the script;
                            a playlist passes its own folder so that all of its
                            tracks end up together in one place.
        base_name         - file name stem to use instead of one derived from
                            the video title. A playlist passes it so the names
                            are known before the download and finished tracks
                            can be recognised on a rerun.

    Returns list of created segment paths.
    """
    temp_dir_path = None  # holds the path to the temporary directory

    try:
        # Download audio (title, description and duration come with the file)
        logger.info("Downloading audio...")
        audio_path, title, description, duration, chapters, temp_dir_path = download_audio(url)
        logger.info(f"Video title: {title}")
        logger.info(f"Video duration: {duration}s")

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
            # Normal for a single song: nothing to split, so keep it whole.
            logger.info("No cut points found - keeping the audio as a single file.")

        # Split audio. On its own a video gets a folder of its own; as part of
        # a playlist it is written straight into the playlist's folder, so the
        # whole playlist stays together instead of becoming hundreds of folders.
        base_name = base_name or sanitize_name(title)
        output_dir = output_dir or base_name
        os.makedirs(output_dir, exist_ok=True)

        logger.info("Splitting audio into segments...")
        created_segments = split_audio(audio_path, timestamps, duration, base_name, output_dir)

        logger.info(f"Done: {len(created_segments)} segments in {output_dir}/")
        return created_segments

    except Exception as e:
        # The caller turns this into the one line the console gets.
        logger.info(f"Error: {e}")
        raise
    finally:
        # Clean up the temporary files
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                shutil.rmtree(temp_dir_path)
                logger.info(f"Temporary files removed: {temp_dir_path}")
            except Exception as cleanup_error:
                logger.warning(f"Could not remove the temporary files: {cleanup_error}")

def extract_playlist_id(url):
    """
    Return the value of the `list=` parameter, or None when the URL has none.
    """
    try:
        query = urllib.parse.urlparse(url).query
    except ValueError:
        return None
    values = urllib.parse.parse_qs(query).get('list') or []
    return values[0] if values and values[0] else None


def fetch_playlist_entries(url):
    """
    List every video of a playlist.

    yt-dlp walks the playlist pagination on its own, so this returns the whole
    list even for playlists whose web page only renders the first 100-200 items
    until you scroll. Extraction is flat: one request chain for the listing, no
    per-video metadata, so a 400+ track playlist takes a few seconds.

    Returns:
        (playlist title, [{'id', 'title', 'url', 'duration'}, ...], number of
        unavailable entries). A URL that is not a playlist yields (None, [], 0).
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': 'in_playlist',
        'socket_timeout': 60,
        'retries': 10,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info or info.get('_type') != 'playlist':
        return None, [], 0

    entries = []
    unavailable = 0
    for entry in (info.get('entries') or []):
        # Deleted and private videos come back as stubs without a title.
        if not entry or not entry.get('id') or not entry.get('title'):
            unavailable += 1
            continue
        entries.append({
            'id': entry['id'],
            'title': entry['title'],
            'url': entry.get('url') or f"https://www.youtube.com/watch?v={entry['id']}",
            # Flat listings carry the duration, which is what the size of the
            # whole playlist can be estimated from before anything is fetched.
            'duration': entry.get('duration'),
        })
    return info.get('title'), entries, unavailable


def prompt_playlist(playlist_title, count):
    """
    Ask what to do with a URL that points into a playlist.
    Returns True for the whole playlist, False for the single video.
    """
    print()
    print(f'This URL belongs to a playlist: "{playlist_title}" ({count} tracks).')
    print("  [Enter] - download the whole playlist")
    print("  o       - only the video the URL points at")
    return input("Choice: ").strip().lower() not in ('o', 'one', 'single', 'n', 'no')


def track_file_pattern(base_name):
    """
    Matches the files one track leaves behind: '<base_name>.mp3' when it was
    not split, '<base_name>_0001.mp3' and so on when it was. Anchored at both
    ends, so one track's name cannot match another's ('Song' must not answer
    for 'Song_Extended').
    """
    return re.compile(re.escape(base_name) + r'(_\d{4})?\.mp3$', re.IGNORECASE)


def track_files(directory, base_name):
    """
    The files already in the folder for this track, in name order.
    """
    pattern = track_file_pattern(base_name)
    try:
        return sorted(f for f in os.listdir(directory) if pattern.match(f))
    except OSError:
        return []


def track_is_done(directory, base_name):
    """
    Whether this track is already in the folder, so a rerun can skip it: on a
    long playlist a dropped connection should not mean starting over. All the
    tracks share one folder, so the match is on this track's own names.
    """
    return bool(track_files(directory, base_name))


def remove_track_files(directory, base_name):
    """
    Delete a track's files. Used when a run is interrupted part way through a
    track: what is on disk is incomplete, and leaving it would make the next
    run treat the track as finished.
    """
    for name in track_files(directory, base_name):
        try:
            os.remove(os.path.join(directory, name))
            logger.debug(f"Removed incomplete file {name}")
        except OSError as e:
            logger.warning(f"Could not remove {name}: {e}")


def track_base_name(position, title, digits=3):
    """
    The file name for a playlist track: its place in the playlist, then the
    title - '001-Sun_City_-_Restless_Heart'. The number first is what keeps a
    folder of several hundred tracks in playlist order in any file manager.
    """
    return f"{position:0{digits}d}-{sanitize_name(title)}"


def number_width(total):
    """
    Digits enough for the largest position, three at the least, so 426 tracks
    are 001..426 and a thousand are 0001..1000.
    """
    return max(3, len(str(total)))


def renumber_existing_tracks(root, entries, digits=3):
    """
    Give files downloaded before the numbering existed the number they now
    carry, so a rerun recognises them instead of fetching them again.

    Returns the number of files renamed.
    """
    renamed = 0
    for position, entry in enumerate(entries, 1):
        old_base = sanitize_name(entry['title'])
        new_base = track_base_name(position, entry['title'], digits)
        if old_base == new_base:
            continue

        pattern = track_file_pattern(old_base)
        try:
            names = os.listdir(root)
        except OSError:
            return renamed

        for name in names:
            match = pattern.match(name)
            if not match:
                continue
            target = new_base + (match.group(1) or '') + '.mp3'
            if os.path.exists(os.path.join(root, target)):
                continue
            try:
                os.replace(os.path.join(root, name), os.path.join(root, target))
                renamed += 1
                logger.debug(f"Renamed {name} -> {target}")
            except OSError as e:
                logger.warning(f"Could not rename {name}: {e}")

    if renamed:
        logger.info(f"Numbered {renamed} file(s) from earlier runs")
    return renamed


def flatten_playlist_folder(root):
    """
    Move segments out of per-track subfolders into the playlist folder.

    Earlier versions gave every track its own subfolder. The files are named
    after the track either way, so they can simply be moved up - and they have
    to be, or a rerun would not recognise them and would download them again.
    """
    try:
        names = os.listdir(root)
    except OSError:
        return 0

    moved = 0
    for name in names:
        sub = os.path.join(root, name)
        if not os.path.isdir(sub):
            continue
        for f in os.listdir(sub):
            if not f.lower().endswith('.mp3'):
                continue
            target = os.path.join(root, f)
            if os.path.exists(target):
                continue  # already up here; leave both alone
            try:
                os.replace(os.path.join(sub, f), target)
                moved += 1
                logger.debug(f"Moved {f} out of {name}/")
            except OSError as e:
                logger.warning(f"Could not move {f} out of {name}/: {e}")
        try:
            os.rmdir(sub)  # only succeeds once it is empty
        except OSError:
            pass

    if moved:
        logger.info(f"Moved {moved} file(s) out of per-track subfolders into {root}/")
    return moved


def _read_key():
    """
    Read one keypress if any is waiting, without blocking. Returns the key in
    lower case, or None.

    Windows only, through msvcrt; everywhere else, and when the console cannot
    be read (output redirected to a file, for instance), the STOP file is the
    way to stop a run.
    """
    try:
        import msvcrt
    except ImportError:
        return None
    try:
        if not msvcrt.kbhit():
            return None
        return msvcrt.getwch().lower()
    except Exception:
        return None


def stop_requested(directory='.'):
    """
    Whether a file named STOP has appeared, which asks a playlist run to stop
    at the next track boundary.

    Keys are the direct way, but they need a console that can be reached and
    read, which is not a given for a run of several hundred tracks started
    hours earlier. Creating a file works from anywhere.
    """
    return os.path.exists(os.path.join(directory, STOP_FILE))


def poll_keys():
    """
    Pick up a key press and acknowledge it at once.

    Called from the download progress hook as well as between tracks: a key
    pressed while a track is downloading would otherwise sit unread until that
    track finished, with nothing on screen to show it had been noticed.

    Returns 'stop', 'pause' or None - whatever is waiting to be acted on.
    """
    global _pending_control
    key = _read_key()

    if key in ('s', 'q') and _pending_control != 'stop':
        _pending_control = 'stop'
        logger.info(f"Stop requested with [{key}]")
        _line("[s] stop requested - finishing this track first")
    elif key == 'p' and _pending_control is None:
        _pending_control = 'pause'
        logger.info("Pause requested with [p]")
        _line("[p] pause requested - finishing this track first")

    return _pending_control


def check_controls(directory='.'):
    """
    Look for a pause or stop request between tracks. Returns 'stop' to end the
    run, or None to carry on; while paused it waits here.

    Keys pressed during a download are buffered by the console, so they take
    effect as soon as the track in progress is finished - which is the point:
    stopping half way through a track would leave a part-written one behind.
    """
    global _pending_control
    pending = poll_keys()
    _pending_control = None

    if _console_lost:
        return 'stop'

    if pending == 'stop' or stop_requested(directory):
        if pending != 'stop':
            logger.info(f"Stop requested by the {STOP_FILE} file")
        _line("stopping")
        return 'stop'

    if pending == 'pause':
        # Logged, not just printed: a run sitting still is the first thing to
        # explain afterwards, and the console scrollback is not evidence.
        logger.info("Paused by the user")
        _line("")
        _print("*" * 52)
        _print("***  PAUSED  ***   [p] carry on   [s] stop")
        _print("*" * 52)
        while True:
            key = _read_key()
            if key == 'p':
                logger.info("Resumed by the user")
                _print("carrying on")
                return None
            if key in ('s', 'q') or stop_requested(directory) or _console_lost:
                logger.info("Stop requested while paused")
                _print("stopping")
                return 'stop'
            time.sleep(0.2)

    return None


# Set when writing to the console fails. A closed window takes the child
# processes with it while python carries on, which is how a run once spent
# four hundred tracks failing at everything.
_console_lost = False


def _write(text, end=chr(10)):
    """
    Write to the console, once. If the console has gone - the window was
    closed, the pipe at the other end is dead - say so in the log and stop
    writing, rather than let an OSError out of a print and end the run with a
    traceback.
    """
    global _console_lost
    if _console_lost:
        return
    try:
        print(text, end=end, flush=True)
    except (OSError, ValueError) as e:
        _console_lost = True
        logger.warning(f"The console cannot be written to any more ({e}); "
                       "the run will stop after this track")


def _console_safe(text):
    """
    Text this console can actually encode.

    A track title can hold characters the console's code page has no room for
    (Korean on a cp866 console), and print would raise UnicodeEncodeError -
    from outside the per-track error handling, so the whole run would end over
    a track that downloaded perfectly well.
    """
    encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    try:
        return text.encode(encoding, 'replace').decode(encoding, 'replace')
    except (LookupError, UnicodeError):
        return text.encode('ascii', 'replace').decode('ascii')


def _print(text=''):
    """
    print() that can neither fall over an unencodable character nor over a
    console that is no longer there.
    """
    _write(_console_safe(text))


def _console_width():
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _status(text):
    """
    Write the self-overwriting status line, so progress does not scroll a line
    per update.
    """
    width = max(_console_width() - 1, 20)
    _write('\r' + _console_safe(text)[:width].ljust(width), end='')


def _line(text):
    """
    Finish the status line and leave text in its place.
    """
    width = max(_console_width() - 1, 20)
    _write('\r' + _console_safe(text)[:width].ljust(width))


def process_playlist(playlist_title, entries, unavailable=0, manual_timestamps=None,
                     mode=MODE_SILENCE, silence_opts=None, overwrite=False,
                     max_failures=DEFAULT_MAX_FAILURES,
                     attempts=DEFAULT_TRACK_ATTEMPTS,
                     min_free_mb=DEFAULT_MIN_FREE_MB):
    """
    Process every video of a playlist. Each track goes through the same
    pipeline as a single video, so its pauses are analysed the same way, and
    all the tracks are written into one folder named after the playlist.

    The console keeps one status line and one line per finished track; the
    details go to the log file.

    The size of the whole playlist is estimated from the listing and shown
    against the free space, and the run stops rather than leaving the disk with
    less than min_free_mb on it.

    A track is tried attempts times before being counted as failed: YouTube
    picks a different player client each extraction and some of them answer 403
    on the media URL, so a second go at the same track often just works.

    A failing track does not stop the run, but max_failures in a row does:
    once the downloads stop working - an expired YouTube token, a dead network,
    a console window closed from under the subprocesses - there is nothing to
    be gained from putting several hundred tracks through the same failure.

    Tracks already on disk are skipped unless overwrite is set, so a stopped
    run resumes where it left off. It can be stopped at a track boundary with
    [s], [q] or a file named STOP, paused with [p], or ended at once with
    Ctrl+C; the track in progress is then rolled back so a rerun redoes it.

    Returns list of created segment paths.
    """
    root = sanitize_name(playlist_title or 'playlist')
    os.makedirs(root, exist_ok=True)
    digits = number_width(len(entries))
    flatten_playlist_folder(root)
    renumber_existing_tracks(root, entries, digits)

    total = len(entries)
    logger.info(f"Playlist: {playlist_title} - {total} tracks, output folder {root}/")
    if unavailable:
        logger.warning(f"Skipped unavailable entries: {unavailable} (deleted or private)")
    if manual_timestamps:
        logger.warning("Manual timestamps are applied to every track of the playlist")

    needed = estimate_download_size(entries)
    free = free_space(root)

    _print(f"Playlist: {playlist_title}")
    _print(f"{total} tracks -> {root}/")
    if needed:
        room = f"About {format_size(needed)} needed"
        if free is not None:
            room += f", {format_size(free)} free"
        logger.info(room)
        _print(room)
        if free is not None and free < needed:
            short = (f"That is more than the disk has - about "
                     f"{format_size(needed - free)} short")
            logger.warning(short)
            _print("*** " + short)
    if unavailable:
        _print(f"({unavailable} unavailable entries skipped)")
    print("Keys: [p] pause   [s] stop after this track   [Ctrl+C] stop now")
    print(f"      (or create a file named {STOP_FILE} in the folder you ran this from)")
    print()

    created = []
    failed = []
    skipped = 0
    stopped_at = None
    stop_reason = None
    in_a_row = 0

    for i, entry in enumerate(entries, 1):
        # The file name is fixed here rather than taken from the title fetched
        # at download time: the two can differ, and then a rerun would
        # re-download a track that is already on disk. It carries the track's
        # place in the playlist, so the folder stays in playlist order.
        track_name = track_base_name(i, entry['title'], digits)
        left = total - i

        if check_controls() == 'stop':
            stopped_at, stop_reason = i, 'asked to stop'
            break

        # Checked every track: an estimate made at the start cannot know what
        # else fills the disk meanwhile, and a disk with nothing left on it is
        # a far worse way to find out.
        if min_free_mb:
            # Not 'left': that is how many tracks are still to come.
            space = free_space(root)
            if space is not None and space < min_free_mb * 1024 * 1024:
                stopped_at = i
                stop_reason = f"only {format_size(space)} left on disk"
                logger.error(f"Stopping before track {i}: {stop_reason}")
                _line(f"[{i}/{total}] stopping - {stop_reason}")
                break

        if not overwrite and track_is_done(root, track_name):
            skipped += 1
            logger.info(f"[{i}/{total}] already done, skipping: {entry['title']}")
            continue

        logger.info(f"[{i}/{total}] {entry['title']}")
        last_error = None

        for attempt in range(1, max(attempts, 1) + 1):
            suffix = f" (attempt {attempt}/{attempts})" if attempt > 1 else ""
            _status(f"[{i}/{total}] {left} left | downloading: "
                    f"{entry['title']}{suffix}")
            try:
                files = process_youtube_video(entry['url'], manual_timestamps, mode,
                                              silence_opts, output_dir=root,
                                              base_name=track_name)
            except KeyboardInterrupt:
                logger.warning(f"Interrupted during {entry['title']} - removing its "
                               "partial files so a rerun redoes it")
                remove_track_files(root, track_name)
                stopped_at, stop_reason = i, 'Ctrl+C'
                _line(f"[{i}/{total}] {entry['title']} - interrupted, rolled back")
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.info(f"[{i}/{total}] attempt {attempt}/{attempts} failed "
                            f"on {entry['title']}: {e}")
                logger.debug(f"Failed track URL: {entry['url']}", exc_info=True)
                # A part-written track would be taken for a finished one.
                remove_track_files(root, track_name)
                if attempt < attempts:
                    time.sleep(TRACK_RETRY_WAIT)
                continue

            created += files
            in_a_row = 0
            last_error = None
            # Written over, not added to: several hundred tracks should not
            # leave several hundred lines behind. The full list is in the log.
            _status(f"[{i}/{total}] {left} left | done: {entry['title']} -> "
                    f"{len(files)} file{'s' if len(files) != 1 else ''}")
            break

        if stopped_at:
            break

        if last_error is not None:
            logger.error(f"[{i}/{total}] gave up on {entry['title']} after "
                         f"{attempts} attempt(s): {last_error}")
            warn_403_once(last_error)
            failed.append(entry['title'])
            in_a_row += 1
            _line(f"[{i}/{total}] {entry['title']} - FAILED: {last_error}")
            if max_failures and in_a_row >= max_failures:
                stopped_at = i
                stop_reason = f"{in_a_row} failures in a row"
                break

    done = (stopped_at if stopped_at else total) - len(failed) - skipped

    _print(f"\n{'='*50}")
    _print(f"Playlist {'stopped' if stopped_at else 'finished'}: {playlist_title}")
    _print(f"Tracks processed: {done}/{total}")
    if skipped:
        _print(f"Already downloaded, skipped: {skipped}")
    _print(f"Segments created: {len(created)}")
    _print(f"Output folder: {root}/")
    if failed:
        _print(f"Failed tracks: {len(failed)}")
        for title in failed[-10:]:
            _print(f"  - {title}")
        if len(failed) > 10:
            _print(f"  ... {len(failed) - 10} more, see the log")
    if stopped_at:
        print(f"Stopped at track {stopped_at} of {total} ({stop_reason}).")
        print("Run the same command again to carry on from there.")
    print(f"{'='*50}")

    logger.info(f"Playlist {'stopped' if stopped_at else 'done'}: {done}/{total} "
                f"processed, {skipped} skipped, {len(failed)} failed, "
                f"{len(created)} segments in {root}/"
                + (f" - stopped at {stopped_at} ({stop_reason})" if stopped_at else ""))
    if failed:
        logger.warning(f"Failed tracks: {'; '.join(failed)}")

    return created


def _process_single(url, manual_timestamps=None, mode=MODE_SILENCE, silence_opts=None):
    """
    One video, with the short console report that a playlist run does per track.
    """
    _status(f"downloading | {url}")
    try:
        created = process_youtube_video(url, manual_timestamps, mode, silence_opts)
    except Exception as e:
        _line("download failed - see the log")
        warn_403_once(e)
        raise
    _line(f"{len(created)} file{'s' if len(created) != 1 else ''} saved"
          + (f" to {os.path.dirname(created[0])}/" if created else ""))
    return created


def process_url(url, manual_timestamps=None, mode=MODE_SILENCE, silence_opts=None,
                playlist_choice=None, interactive=None, overwrite=False,
                max_failures=DEFAULT_MAX_FAILURES,
                attempts=DEFAULT_TRACK_ATTEMPTS,
                min_free_mb=DEFAULT_MIN_FREE_MB):
    """
    Top-level entry point: works out whether the URL means a single video or a
    whole playlist, then hands over to the matching function.

    Parameters:
        playlist_choice - True to take the whole playlist, False for just the
                          single video, None to decide here (ask when the run
                          is interactive, otherwise take the single video:
                          a few hundred tracks should never start unattended).
        interactive     - overrides the automatic detection of an interactive run.
        max_failures    - consecutive track failures that end a playlist run.
        attempts        - tries per track before it counts as failed.
        min_free_mb     - stop rather than leave the disk with less than this.

    Returns list of created segment paths.
    """
    if playlist_choice is False or not extract_playlist_id(url):
        return _process_single(url, manual_timestamps, mode, silence_opts)

    logger.info("The URL points into a playlist, listing its videos...")
    print("Listing the playlist...")
    playlist_title, entries, unavailable = fetch_playlist_entries(url)

    if not entries:
        logger.warning("Could not list the playlist - processing the single video.")
        return _process_single(url, manual_timestamps, mode, silence_opts)

    logger.info(f"Playlist videos found: {len(entries)}")

    if playlist_choice is None:
        if interactive is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        if interactive:
            playlist_choice = prompt_playlist(playlist_title, len(entries))
        else:
            logger.info("Non-interactive run: taking the single video "
                        "(pass --playlist for the whole playlist).")
            playlist_choice = False

    if not playlist_choice:
        return _process_single(url, manual_timestamps, mode, silence_opts)

    needed = estimate_download_size(entries)
    free = free_space('.')
    if needed and free is not None and free < needed:
        logger.warning(f"The playlist needs about {format_size(needed)}, "
                       f"{format_size(free)} free")
        _print(f"This playlist needs about {format_size(needed)} and there is "
               f"{format_size(free)} free.")
        if interactive:
            if input("Start anyway? (y/n): ").strip().lower() not in ('y', 'yes'):
                _print("Nothing downloaded.")
                return []
        else:
            _print("Starting anyway; it will stop before the disk fills.")

    return process_playlist(playlist_title, entries, unavailable, manual_timestamps,
                            mode, silence_opts, overwrite, max_failures, attempts,
                            min_free_mb)


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """
    Shows defaults the way ArgumentDefaultsHelpFormatter does, but stays quiet
    about flags that have no meaningful default: an off switch does not need a
    '(default: False)' note, and the playlist pair defaults to asking.
    """
    def _get_help_string(self, action):
        if action.default is None or action.default is False:
            return action.help
        return super()._get_help_string(action)


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
        formatter_class=_HelpFormatter)
    p.add_argument('url', nargs='?', help='YouTube video URL')
    p.add_argument('-t', '--timestamps', metavar='SEC',
                   help="your own timestamps in seconds, comma-separated, e.g. '145,325'")
    p.add_argument('-d', '--from-description', '--desc', '--chapters',
                   dest='from_description', action='store_true',
                   help='force timestamps from the video chapters/description '
                        'instead of silence analysis')
    p.add_argument('--silence', dest='force_silence', action='store_true',
                   help='explicitly select silence analysis (the default mode)')

    g = p.add_argument_group('playlists')
    g.add_argument('--playlist', dest='playlist', action='store_true', default=None,
                   help='for a URL with a list= parameter, take every track of '
                        'the playlist without asking')
    g.add_argument('--no-playlist', dest='playlist', action='store_false',
                   default=None,
                   help='for a URL with a list= parameter, take only the video '
                        'it points at without asking')
    g.add_argument('--attempts', type=int, default=DEFAULT_TRACK_ATTEMPTS,
                   metavar='N',
                   help='tries per track before it counts as failed; YouTube '
                        'answers 403 on some of its own player clients, and a '
                        'second go usually lands on a working one')
    g.add_argument('--min-free', type=int, default=DEFAULT_MIN_FREE_MB,
                   metavar='MB',
                   help='stop the run rather than leave the disk with less '
                        'free space than this; 0 turns the check off')
    g.add_argument('--max-failures', type=int, default=DEFAULT_MAX_FAILURES,
                   metavar='N',
                   help='end a playlist run after this many track failures in '
                        'a row; 0 never gives up')
    g.add_argument('--overwrite', action='store_true',
                   help='re-download playlist tracks whose folder already holds '
                        'mp3 files (by default they are skipped, so an '
                        'interrupted playlist run resumes)')

    g = p.add_argument_group('logging')
    g.add_argument('--log-file', default=DEFAULT_LOG_FILE, metavar='PATH',
                   help='write a full-detail log to this file; strftime '
                        'placeholders are expanded, so the default gives one '
                        'file per run')
    g.add_argument('--no-log-file', dest='log_file', action='store_const', const=None,
                   help='do not write a log file')
    g.add_argument('-v', '--verbose', action='store_true',
                   help="show debug output on the console and ask yt-dlp for its own")

    g = p.add_argument_group('downloading')
    g.add_argument('--remote-components', metavar='COMPONENT', action='append',
                   help="let yt-dlp fetch a remote component when it needs one, "
                        "e.g. 'ejs:github' for YouTube's JS challenge solver. It "
                        "downloads and runs code from that source at run time, so "
                        "it is off by default; 'pip install yt-dlp-ejs' is the "
                        "local alternative. Repeat the flag for several components")

    g = p.add_argument_group('silence analysis settings')
    g.add_argument('--noise', default=DEFAULT_NOISE, metavar='dB',
                   help='volume threshold below which sound counts as silence; '
                        'the minus sign may be omitted: --noise 30dB or --noise=-30dB')
    g.add_argument('--min-silence', type=float, default=1.5, metavar='SEC',
                   help='starting pause-length threshold')
    g.add_argument('--floor', type=float, default=0.8, metavar='SEC',
                   help='lowest pause-length threshold when lowering')
    g.add_argument('--max-segment', type=float, default=DEFAULT_MAX_SEGMENT,
                   metavar='SEC',
                   help='a piece longer than this is re-read at a lower pause '
                        'threshold and, failing that, cut where no pause was; '
                        '0 means only ever cut at a real pause')
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
        return DEFAULT_NOISE
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

    print_banner()
    log_path = setup_logging(args.log_file, args.verbose)
    _remote_components = args.remote_components or []
    log_environment()
    clean_stale_temp_dirs()
    if log_path:
        print(f"Log file: {log_path}")

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
        process_url(url, manual_timestamps, mode=mode, silence_opts=silence_opts,
                    playlist_choice=args.playlist, overwrite=args.overwrite,
                    max_failures=args.max_failures, attempts=args.attempts,
                    min_free_mb=args.min_free)
    except KeyboardInterrupt:
        logger.info("\nOperation cancelled by the user")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        logger.debug("Traceback", exc_info=True)
        print(f"\n[ERROR] {e}")
        print("Possible fixes:")
        print("  1. Check your internet connection")
        print("  2. Update yt-dlp: pip install -U yt-dlp")
        print("  3. Update imageio-ffmpeg: pip install -U imageio-ffmpeg")
        print("  4. Try a different YouTube link")
        if log_path:
            print(f"  Details of this run are in {log_path}")
