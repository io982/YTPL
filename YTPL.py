import yt_dlp
import re
import os
import subprocess
import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup

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
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': temp_dir + '.%(ext)s',
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
        downloaded_file = temp_dir + '.' + ext
        mp3_file = temp_dir + '.mp3'
        if ext.lower() == 'mp3':
            mp3_file = downloaded_file  # already mp3
        else:
            # Convert to mp3 using ffmpeg
            cmd = [ffmpeg_path, '-i', downloaded_file, '-q:a', '0', '-y', mp3_file]
            subprocess.run(cmd, check=True)
            # Remove original
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)
    return mp3_file, title, description, duration

def split_audio(audio_path, timestamps, duration, base_name='видео', output_dir='.'):
    """
    Split audio into segments based on timestamps using ffmpeg.
    """
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    segments = []
    start = 0
    for i, end in enumerate(timestamps):
        if end > start:
            segments.append((start, end))
            start = end

    if start < duration:
        segments.append((start, duration))

    for i, (start_time, end_time) in enumerate(segments):
        print(f"Processing segment {i+1}: {start_time}s - {end_time}s")
        segment_num = f"{i+1:04d}"
        output_filename = os.path.join(output_dir, f"{base_name}_{segment_num}.mp3")
        duration_seg = end_time - start_time
        cmd = [ffmpeg_path, '-i', audio_path, '-ss', str(start_time), '-t', str(duration_seg), '-c', 'copy', output_filename]
        subprocess.run(cmd, check=True)
        print(f"Saved: {output_filename}")

def process_youtube_video(url, manual_timestamps=None):
    """
    Main function to process YouTube video.
    """
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
                return

        # Download audio
        temp_audio = 'temp_audio.mp3'
        audio_path, _, _, _ = download_audio(url, temp_audio)

        # Split audio
        base_name = re.sub(r'[^\w\-_\. ]', '_', title).replace(' ', '_')
        output_dir = base_name
        os.makedirs(output_dir, exist_ok=True)
        split_audio(audio_path, timestamps, duration, base_name, output_dir)

        # Clean up
        if os.path.exists(temp_audio):
            os.remove(temp_audio)
        print("Processing completed successfully!")

    except Exception as e:
        print(f"Error: {e}")
        if os.path.exists('temp_audio.mp3'):
            os.remove('temp_audio.mp3')

if __name__ == "__main__":
    url = input("Enter YouTube video URL: ").strip()
    manual_input = input("Enter manual timestamps (comma-separated in seconds, e.g., '145,325'), or leave empty to extract from description: ").strip()

    manual_timestamps = manual_input if manual_input else None

    process_youtube_video(url, manual_timestamps)
