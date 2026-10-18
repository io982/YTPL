# YTPL - YouTube To PlayList converter

<div align="center">
<pre>
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
</pre>
</div>

A Python script that downloads audio from a YouTube video and splits it into
separate tracks.

**By default it splits by silence gaps in the audio and ignores the video's
chapters and description.** The reason: on mixes and compilations, author
timestamps usually mark large blocks rather than individual tracks - a
108-minute mix with five "Sector I-V" chapters would be cut into five 20-minute
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
- **Playlists**: a `list=` URL offers the whole playlist or just the one
  video; every track is then checked for pauses the same way
- **Full log file** (`ytpl.log`) with yt-dlp's own output and tracebacks
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
- Node.js - recommended: yt-dlp uses it to decrypt YouTube URLs, otherwise
  downloads are throttled and time out
- yt-dlp-ejs - YouTube's JS challenge solver; without it downloads fail with
  HTTP 403
- yt-dlp 2026.8.19 or newer - 2026.07 answered 403 on every download
- pytest - only to run the tests

## Installation

```bash
pip install -r requirements.txt
```

Node.js is needed too and is not a Python package: get it from
<https://nodejs.org>. For the tests, `pip install -r requirements-dev.txt`.

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
| `--playlist` | for a `list=` URL, take every track without asking |
| `--no-playlist` | for a `list=` URL, take only the video it points at |
| `--overwrite` | re-download playlist tracks that are already on disk |
| `--attempts 3` | tries per track before it counts as failed |
| `--max-failures 5` | end the run after this many failures in a row; 0 never gives up |
| `--log-file PATH` | where to write the log (default `ytpl.log`) |
| `--no-log-file` | do not write a log file |
| `-v`, `--verbose` | debug output on the console, and yt-dlp's own |
| `--remote-components ejs:github` | let yt-dlp fetch the JS challenge solver at run time |
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

## Playlists

A URL carrying a `list=` parameter points into a playlist. YTPL then offers a
choice:

```
This URL belongs to a playlist: "Essential NewRetroWave Vocal Jams" (426 tracks).
  [Enter] - download the whole playlist
  o       - only the video the URL points at
```

Use `--playlist` or `--no-playlist` to answer in advance. A **non-interactive
run takes only the single video** unless `--playlist` is given: a few hundred
tracks should never start downloading unattended.

**The whole playlist is listed, not just the part a browser shows.** The web
page loads around 200 items and fetches more as you scroll, but yt-dlp walks
that pagination itself, so a 429-entry playlist comes back complete in a few
seconds. The listing is flat (no per-video metadata), which is what keeps it
that fast. Deleted and private entries come back without a title; they are
counted and skipped.

Each track is then processed exactly like a single video, so the pauses inside
it are analysed the same way. Everything lands in one folder named after the
playlist:

```
Essential_NewRetroWave_Vocal_Jams/
  Sun_City_-_Restless_Heart_0001.mp3
  Thought_Beings_-_Sky_High_0001.mp3
  Thought_Beings_-_Sky_High_0002.mp3
```

A track already on disk is skipped, so a stopped run resumes where it left off;
`--overwrite` downloads it again. (Earlier versions gave every track its own
subfolder; those files are moved up into the playlist folder on the next run,
so nothing is downloaded twice.)

Each track gets `--attempts` tries, 3 by default. This is not padding: YouTube
hands out a different player client on each extraction and some of them answer
403 on the media URL, so the second go at the very same track usually just
works. A track that fails every attempt does not stop the run - but
`--max-failures` in a row does, because downloads that stop working stay
broken, and there is nothing to be gained from putting another 400 tracks
through the same failure.

### While it runs

The console keeps one status line plus a line per finished track, and the
detail goes to the log file:

```
Playlist: Essential NewRetroWave Vocal Jams
426 tracks -> Essential_NewRetroWave_Vocal_Jams/
Keys: [p] pause   [s] stop after this track   [Ctrl+C] stop now

[1/426] Sun City - Restless Heart -> 1 file
[2/426] Thought Beings - Sky High -> 2 files
[3/426] 423 left | Subject Sue - Details in the Dark
```

| Key | Effect |
| --- | --- |
| `p` | pause after the current track, `p` again to carry on |
| `s` or `q` | stop after the current track |
| `Ctrl+C` | stop now; the track in progress is rolled back |

Keys pressed during a download take effect when that track finishes, which is
the point: stopping half way through would leave a part-written track behind.
A file named `STOP` next to the program does the same as `s`, for when the
console cannot be reached. Either way the summary says where it stopped, and
running the same command again carries on from there.

### One file per track

A track with a long enough pause inside it is split, which is usually what you
want for a mix but not always for a song. To keep every track whole, set a
minimum segment longer than any track:

```bash
python YTPL.py "<url>" --playlist --min-segment 600
```

## Logging

Every run writes its own log file, named after the moment it started:

```
logs/ytpl-20261018-172847.log
```

So one file is one session - nothing is appended to a shared log, and the file
you want to read or send is exactly the run you just made. `--log-file` takes
another path and expands strftime placeholders of its own
(`--log-file "run-%Y%m%d.log"` gives one file per day); `--no-log-file` switches
the file off.

The console stays readable while the file records everything at DEBUG: the environment banner (Python, platform, yt-dlp
and ffmpeg versions), yt-dlp's own messages, the resolved format, and full
tracebacks. A console line like

```
ERROR - Download failed for https://...: HTTP Error 403: Forbidden
```

has its whole traceback and the yt-dlp exchange that led to it in the file.
`-v` additionally shows the debug lines on the console and asks yt-dlp for its
own debug output.

## Mode priority

1. `--timestamps` - your own values; they override everything.
2. `--from-description` - chapters -> description -> page HTML. If there are no
   timestamps there, falls back to silence analysis.
3. Default - silence analysis; metadata is not read at all.

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

## When every download fails with HTTP 403

This was seen for real: a 426-track playlist run where every single track
answered `HTTP Error 403: Forbidden`. Two separate things were missing, and
both had to be fixed.

**1. The JS challenge solver.** YouTube requires its `n` challenge to be
solved, which needs Node.js *and* a solver script. Node alone is not enough;
without the script the log shows

```
n challenge solving failed: Some formats may be missing.
```

**2. A current yt-dlp.** With the solver installed the challenge warning went
away, but 403 persisted on yt-dlp 2026.07.04. The same track downloaded
normally on 2026.08.19, so `requirements.txt` sets that as the floor.

Both are covered by

```bash
pip install -r requirements.txt --upgrade
```

The solver's presence is checked at startup and reported in the log, and every
403 is answered with what to install, so neither has to be guessed at again.
Instead of installing the solver, yt-dlp can be allowed to fetch one at run
time with `--remote-components ejs:github`; that downloads and runs code from
an external source while the program works, so it is off by default.

## Error Handling

- `--from-description` with no timestamps in metadata falls back to silence analysis
- No cut points at all: the audio is kept as a single file (and a
  non-interactive run does not hang waiting for an answer)
- Invalid URLs and missing dependencies are reported clearly
- Timestamps are validated; invalid and overlapping ones are skipped
- Temporary files are cleaned up on exit and on error
- ffmpeg availability is checked before processing

## Changelog of the latest version

- **Playlist support**: a `list=` URL offers the whole playlist or the single
  video; the whole listing is enumerated (not only the ~200 items a browser
  shows), unavailable entries are skipped, finished tracks are skipped on a
  rerun, and a failing track does not stop the run
- **Fixed: a `list=` URL used to crash**, because yt-dlp returned the playlist
  rather than the video and the result has no `duration` field. Single-video
  downloads now pass `noplaylist`
- **Logging**: one full-detail log file per run under `logs/`, named after the
  start of the session, with the environment banner, yt-dlp's own output routed
  into the log, tracebacks in the file and one line on the console, plus
  `--log-file` / `--no-log-file` / `-v`
- **Greeting banner** on startup; a terminal narrower than 84 columns gets a
  plain title line instead of wrapped art
- **A missing JS challenge solver is now reported** at startup and on every 403,
  instead of leaving hundreds of tracks to fail without a stated reason;
  `--remote-components` allows a remote one
- **Fixed: a wedged `node --version` could hang the whole run.** Node is probed
  once with a timeout, and the run carries on without the runtime if it does
  not answer
- **No more prompt when there is nothing to cut.** A track without internal
  pauses is the normal case for a single song, the audio is already downloaded
  by then, and the question crashed a run whose stdin was at EOF (never mind
  asking it 426 times during a playlist)
- **Fixed: every failed download leaked its temporary folder** - the caller
  never received the path, so nothing cleaned it up. One failed playlist run
  had left 134 of them behind
- **`requirements.txt` / `requirements-dev.txt`** instead of a pip line in the
  README
- **One folder per playlist**, not per track; files from the old layout are
  moved up on the next run so nothing is downloaded twice
- **A quiet console**: a status line with the total, what is left and the
  current track, a line per finished track, and everything else in the log
- **Pause and stop**: `p`, `s`/`q`, `Ctrl+C` or a `STOP` file, all at a track
  boundary, with the track in progress rolled back and the summary saying
  where to carry on from
- **Each track is retried** (`--attempts`), because YouTube answers 403 on
  some of its own player clients and the next try lands on a working one
- **A run that has stopped working now gives up** (`--max-failures`) instead
  of putting hundreds of tracks through the same failure
- **Fixed: ffmpeg's progress meter was printed over the console's own output**
  during the conversion to mp3

- **Silence analysis is now the default; chapters and description are used only
  with `--from-description`**
- **Command-line interface**: URL and mode can be passed as arguments, so the
  script can run non-interactively; `YTPL.bat` forwards its arguments
- **Fixed: audio shorter than `--max-segment` (5 minutes) was not split at all,
  even with obvious gaps between tracks** - the first pass at the starting
  threshold now always runs
- `--noise` accepts a value without the minus sign (argparse would otherwise
  read `-30dB` as a flag)
- pytest test suite
- UTF-8 console code page in `YTPL.bat`, so video titles with non-ASCII
  characters print correctly on Windows
