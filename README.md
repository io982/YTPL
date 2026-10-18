# YTPL - YouTube To PlayList converter

A Python script that downloads audio from a YouTube video and splits it into
separate tracks.

**By default it splits by silence gaps in the audio and ignores the video's
chapters and description.** The reason: on mixes and compilations, author
timestamps usually mark large blocks rather than individual tracks — a
108-minute mix with five "Sector I…V" chapters would be cut into five 20-minute
files, each containing several tracks. Metadata is used only when you ask for it
explicitly with `--from-description`.

## Features

- Downloads audio from YouTube via yt-dlp
- **Splits by silence gaps between tracks by default (`silencedetect`)**
- Recursive threshold lowering: starts at 1.5 s pauses and goes down to 0.8 s
  while any piece is still longer than `--max-segment`; whatever stays too long
  is split evenly by that length
- Short tracks are merged into their neighbour (`--min-segment`), so no stray
  fragments
- **Chapters and description timestamps on demand (`--from-description`)**:
  YouTube chapters first, then description regexes, then page HTML
- Manual timestamps (`--timestamps 145,325`)
- Timestamp formats: `&t=145s`, `?t=2m25s`, `=1102s` in URLs, `MM:SS`, `HH:MM:SS`
- Splits into MP3 segments with ffmpeg, sanitized filenames
- Timestamp validation (invalid/overlapping ones are skipped)
- Logging, temporary-file cleanup, ffmpeg availability check
- pytest test suite

## Requirements

- Python 3.8+
- yt-dlp
- requests
- beautifulsoup4
- imageio-ffmpeg (includes ffmpeg)
- Node.js — recommended: yt-dlp uses it to decrypt YouTube URLs, otherwise
  downloads are throttled and time out
- pytest — only to run the tests

## Installation

```bash
pip install yt-dlp requests beautifulsoup4 imageio-ffmpeg
```

## Usage

Interactive (asks for the URL and the mode; `Enter` keeps the default):

```bash
python YTPL.py
```

Or double-click `YTPL.bat` (Windows).

Non-interactive:

```bash
python YTPL.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Passing a URL as an argument runs the default mode without any questions.

### Options

| Option | Meaning |
| --- | --- |
| `-d`, `--from-description` | force timestamps from chapters/description instead of silence analysis (aliases: `--desc`, `--chapters`) |
| `-t`, `--timestamps 145,325` | your own cut points in seconds |
| `--silence` | explicitly select the default mode |
| `--noise 30dB` | silence volume threshold (default `-30dB`; the minus sign may be omitted, or use `--noise=-30dB`) |
| `--min-silence 1.5` | starting pause-length threshold, seconds |
| `--floor 0.8` | lowest pause-length threshold, seconds |
| `--max-segment 300` | a piece longer than this gets re-split with a lower threshold |
| `--min-segment 15` | minimum track length; shorter pieces are merged into the neighbour |

Examples:

```bash
python YTPL.py "https://youtu.be/VIDEO_ID" --from-description
python YTPL.py "https://youtu.be/VIDEO_ID" --min-segment 60 --max-segment 420
python YTPL.py "https://youtu.be/VIDEO_ID" -t 0,185,420,735
```

## Mode priority

1. `--timestamps` — your own values; they override everything.
2. `--from-description` — chapters → description → page HTML. If there are no
   timestamps there, falls back to silence analysis.
3. Default — silence analysis; metadata is not read at all.

## Output

Files are saved in a folder named after the video title:
`{video_title}/{video_title}_{segment_number:04d}.mp3`

For example: `My_Video/My_Video_0001.mp3`, `My_Video/My_Video_0002.mp3`

## Tests

```bash
pip install pytest
python -m pytest
```

The suite needs neither the network nor YouTube: the download is stubbed and the
test audio is generated locally with ffmpeg. It covers timestamp parsing, the
silence algorithm (gaps, threshold lowering, hard splitting, short-piece
merging), mode selection and the actual ffmpeg splitting.

## Error Handling

- `--from-description` with no timestamps in metadata falls back to silence analysis
- No cut points at all: the audio is kept as a single file (and a
  non-interactive run does not hang waiting for an answer)
- Invalid URLs and missing dependencies are reported clearly
- Timestamps are validated; invalid and overlapping ones are skipped
- Temporary files are cleaned up on exit and on error
- ffmpeg availability is checked before processing

## Changelog of the latest version

- **Silence analysis is now the default; chapters and description are used only
  with `--from-description`**
- **Command-line interface**: URL and mode can be passed as arguments, so the
  script can run non-interactively; `YTPL.bat` forwards its arguments
- **Fixed: audio shorter than `--max-segment` (5 minutes) was not split at all,
  even with obvious gaps between tracks** — the first pass at the starting
  threshold now always runs
- `--noise` accepts a value without the minus sign (argparse would otherwise
  read `-30dB` as a flag)
- pytest test suite
- UTF-8 console code page in `YTPL.bat` (readable log messages)
