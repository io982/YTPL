# YTPL - YouTube To PlayList converter

A Python script to download YouTube videos and split their audio into segments based on timestamps found in the video description or page.

## Features

- Downloads audio from YouTube videos using yt-dlp
- Automatically extracts timestamps from video descriptions and page content
- Supports various timestamp formats: &t=145s, ?t=145s, =1102s in URLs, MM:SS, HH:MM:SS
- Allows manual timestamp input
- Splits audio into MP3 segments using ffmpeg
- Sanitizes filenames for safe file system usage
- **Improved error handling and logging**
- **Automatic cleanup of temporary files**
- **Validation of timestamps (skips invalid/overlapping ones)**

## Requirements

- Python 3.8+
- yt-dlp
- requests
- beautifulsoup4
- imageio-ffmpeg (includes ffmpeg)

## Installation

1. Install Python dependencies:
   ```bash
   pip install yt-dlp requests beautifulsoup4 imageio-ffmpeg
   ```

2. Ensure ffmpeg is available (included with imageio-ffmpeg)

## Usage

Run the script:

```bash
python YTPL.py
```

Enter the YouTube video URL when prompted.

Optionally enter manual timestamps as comma-separated seconds (e.g., '145,325'), or leave empty to auto-extract.

The script will:
- Fetch video information
- Extract timestamps from description and page
- Download and convert audio to MP3
- Split into segments based on timestamps
- Save MP3 files with sanitized names

## Output

Files are saved in a folder named after the video title.

Each segment is saved as: `{video_title}/{video_title}_{segment_number:04d}.mp3`

For example: `My_Video/My_Video_0001.mp3`, `My_Video/My_Video_0002.mp3`

## Supported Timestamp Formats

- URL parameters: `&t=145s`, `?t=2m25s`
- Time codes in text: `1:30`, `1:30:25`
- Custom URL formats: `https://youtu.be/...=1102s`

## Error Handling

- Falls back to full page parsing if description lacks timestamps
- Prompts for manual input if no timestamps found
- Handles invalid URLs and missing dependencies gracefully
- **Validates timestamps and skips invalid/overlapping ones**
- **Automatic cleanup of temporary files on exit or error**
- **FFmpeg availability check before processing**
- **Detailed logging for troubleshooting**

## Improvements in Latest Version

- Temporary files now stored in isolated temp directories (no clutter in working directory)
- Better error messages with timestamps via logging module
- Validation of timestamps before processing
- Graceful handling of keyboard interrupts (Ctrl+C)
- Returns list of created segments for potential programmatic use
