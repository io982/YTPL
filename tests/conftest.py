"""
Shared fixtures for the YTPL tests.

The tests never touch the network: the YouTube download is stubbed, and the
audio used to exercise the silence analysis is generated locally with ffmpeg
(from imageio-ffmpeg).
"""
import os
import subprocess
import sys
import types

import imageio_ffmpeg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import YTPL  # noqa: E402  (needs the sys.path line above)


# Chapters of the example video from the README: five "sectors" over 108
# minutes, so every chapter holds several tracks - exactly the case the default
# mode ignores metadata for.
EXAMPLE_CHAPTERS = [
    {'start_time': 0, 'title': 'Sector I - Press Start (Intro)', 'end_time': 930},
    {'start_time': 930, 'title': 'Sector II - Neon City Streets', 'end_time': 2085},
    {'start_time': 2085, 'title': 'Sector III - Highway Chase Protocol', 'end_time': 3130},
    {'start_time': 3130, 'title': 'Sector IV - 16-Bit Dream State', 'end_time': 4200},
    {'start_time': 4200, 'title': 'Sector V - The Final Boss', 'end_time': 6482},
]

EXAMPLE_DESCRIPTION = """Step into the NEON VAULT.

CHAPTERS:
00:00:00 Sector I - Press Start (Intro)
00:15:30 Sector II - Neon City Streets
00:34:45 Sector III - Highway Chase Protocol
00:52:10 Sector IV - 16-Bit Dream State
01:10:00 Sector V - The Final Boss (Peak Energy)
"""


@pytest.fixture(scope='session')
def ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope='session')
def make_audio(ffmpeg, tmp_path_factory):
    """
    Test-audio factory. Takes a list of pieces:
        ('tone', duration, frequency) - an audible piece,
        ('silence', duration)         - silence.

    Returns (path to the wav, total duration in seconds).
    The sample rate is deliberately low (8 kHz mono): the files stay small and
    silencedetect behaves the same.
    """
    cache = {}
    base = tmp_path_factory.mktemp('audio')

    def _build(parts, name):
        if name in cache:
            return cache[name]

        work = base / name
        work.mkdir()
        pieces = []
        total = 0.0
        for i, part in enumerate(parts):
            piece = work / f'{i:03d}.wav'
            if part[0] == 'tone':
                _, dur, freq = part
                src = f'sine=frequency={freq}:duration={dur}:sample_rate=8000'
            else:
                _, dur = part
                src = 'anullsrc=r=8000:cl=mono'
            cmd = [ffmpeg, '-f', 'lavfi', '-i', src, '-t', str(dur),
                   '-ar', '8000', '-ac', '1', '-y', str(piece)]
            subprocess.run(cmd, capture_output=True, check=True)
            pieces.append(piece)
            total += dur

        listing = work / 'list.txt'
        listing.write_text(''.join(f"file '{p.name}'\n" for p in pieces), encoding='utf-8')
        out = work / 'mix.wav'
        subprocess.run([ffmpeg, '-f', 'concat', '-safe', '0', '-i', str(listing),
                        '-ar', '8000', '-ac', '1', '-y', str(out)],
                       capture_output=True, check=True)

        cache[name] = (str(out), total)
        return cache[name]

    return _build


@pytest.fixture(scope='session')
def album_audio(make_audio):
    """
    An "album": three 40 s tracks with 2 s pauses between them. The total of
    124 s is shorter than max_segment_len (300 s), which is what makes this
    check that the first pass over the pauses runs regardless of piece length.
    Expected cut points: the pause midpoints, i.e. around 41 s and 83 s.
    """
    return make_audio([
        ('tone', 40, 440),
        ('silence', 2),
        ('tone', 40, 660),
        ('silence', 2),
        ('tone', 40, 880),
    ], 'album')


@pytest.fixture(scope='session')
def album_mp3(ffmpeg, album_audio, tmp_path_factory):
    """
    The same "album" as mp3: split_audio cuts without re-encoding (-c copy), so
    it needs mp3 input - just like in real use, where download_audio always
    hands over an mp3.
    """
    path, duration = album_audio
    out = tmp_path_factory.mktemp('mp3') / 'album.mp3'
    subprocess.run([ffmpeg, '-i', path, '-ar', '22050', '-ac', '1',
                    '-b:a', '64k', '-y', str(out)],
                   capture_output=True, check=True)
    return str(out), duration


@pytest.fixture(scope='session')
def gapless_audio(make_audio):
    """
    700 continuous seconds without a single pause: no silence threshold can
    help, so a hard split by max_segment_len is expected.
    """
    return make_audio([('tone', 700, 440)], 'gapless')


@pytest.fixture(scope='session')
def mixed_audio(make_audio):
    """
    Audio with pauses of different lengths: 1.0 s (below the 1.5 s starting
    threshold) and 2.0 s. Only the long gap is visible at the starting
    threshold; the short one is picked up when the threshold is lowered for
    pieces that are too long.
    Layout: 100s | 1s | 100s | 2s | 100s  (303 s in total).
    """
    return make_audio([
        ('tone', 100, 440),
        ('silence', 1.0),
        ('tone', 100, 660),
        ('silence', 2.0),
        ('tone', 100, 880),
    ], 'mixed')


@pytest.fixture
def fake_download(monkeypatch):
    """
    Stubs download_audio and split_audio so the processing runs without the
    network and without writing files. Returns a setup function; after calling
    it, its `.cuts` attribute holds the cut points handed to the splitter.
    """
    state = types.SimpleNamespace(cuts=None, base_name=None, output_dir=None)

    def setup(audio_path, duration, title='Test Mix',
              description=EXAMPLE_DESCRIPTION, chapters=None):
        def _download(url):
            return audio_path, title, description, duration, chapters, None

        def _split(path, timestamps, dur, base_name, output_dir):
            state.cuts = [float(t) for t in timestamps]
            state.base_name = base_name
            state.output_dir = output_dir
            return [f'{base_name}_{i + 1:04d}.mp3' for i in range(len(timestamps) + 1)]

        monkeypatch.setattr(YTPL, 'download_audio', _download)
        monkeypatch.setattr(YTPL, 'split_audio', _split)
        monkeypatch.setattr(YTPL.os, 'makedirs', lambda *a, **k: None)
        # Safety net: metadata must never be fetched over the network in tests.
        monkeypatch.setattr(YTPL, 'get_full_page_text',
                            lambda url: pytest.fail('network request in a test'))
        return state

    setup.state = state
    return setup


@pytest.fixture
def args_factory():
    """
    Builds an arguments object the way argparse would return it.
    """
    def _make(**overrides):
        parser = YTPL.build_parser()
        argv = []
        if overrides.pop('url', None):
            argv.append('https://example.invalid/watch?v=x')
        args = parser.parse_args(argv)
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    return _make
