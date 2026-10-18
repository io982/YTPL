"""
Housekeeping: clearing temp folders left by earlier runs, and surviving text
that the console or the system code page cannot handle.

Both come from real runs: a killed run left 134 temp folders behind, and a
track with a Korean title made ffmpeg print bytes that the system code page
could not decode, which killed subprocess's reader thread mid-download.
"""
import os
import subprocess
import time

import imageio_ffmpeg
import pytest

import YTPL


# --- temp folders ----------------------------------------------------------

@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(YTPL.tempfile, 'gettempdir', lambda: str(tmp_path))
    return tmp_path


def _aged(path, minutes):
    past = time.time() - minutes * 60
    os.utime(path, (past, past))


def test_stale_temp_folders_are_removed(temp_root):
    old = temp_root / (YTPL.TEMP_PREFIX + 'old')
    old.mkdir()
    (old / 'audio.webm').write_bytes(b'x' * 100)
    _aged(old, 60)

    assert YTPL.clean_stale_temp_dirs(older_than_minutes=10) == 1
    assert not old.exists()


def test_a_fresh_temp_folder_is_left_alone(temp_root):
    """
    It may belong to a run that is working right now.
    """
    fresh = temp_root / (YTPL.TEMP_PREFIX + 'fresh')
    fresh.mkdir()

    assert YTPL.clean_stale_temp_dirs(older_than_minutes=10) == 0
    assert fresh.exists()


def test_other_folders_are_not_touched(temp_root):
    other = temp_root / 'pip-build-something'
    other.mkdir()
    _aged(other, 600)

    assert YTPL.clean_stale_temp_dirs(older_than_minutes=10) == 0
    assert other.exists()


def test_files_named_like_temp_folders_are_ignored(temp_root):
    stray = temp_root / (YTPL.TEMP_PREFIX + 'file.txt')
    stray.write_text('not a folder', encoding='utf-8')
    _aged(stray, 600)

    assert YTPL.clean_stale_temp_dirs(older_than_minutes=10) == 0
    assert stray.exists()


def test_cleaning_a_temp_root_that_cannot_be_listed(monkeypatch):
    """
    No temp directory to read is not a reason to stop before downloading.
    """
    monkeypatch.setattr(YTPL.tempfile, 'gettempdir',
                        lambda: os.path.join('no', 'such', 'place'))

    assert YTPL.clean_stale_temp_dirs() == 0


def test_downloads_use_the_known_prefix():
    """
    The sweep finds folders by this prefix, so the two have to agree.
    """
    import inspect
    assert 'prefix=TEMP_PREFIX' in inspect.getsource(YTPL.download_audio)


# --- text the console cannot encode ---------------------------------------

class _Console:
    """
    A stand-in for sys.stdout with a chosen encoding, so the console's own
    limits can be reproduced without one.
    """
    def __init__(self, encoding):
        self.encoding = encoding
        self.written = []

    def write(self, text):
        self.written.append(text)
        return len(text)

    def flush(self):
        pass

    @property
    def text(self):
        return ''.join(self.written)


def test_console_safe_replaces_unencodable_characters(monkeypatch):
    """
    A Korean track title on a cp866 console: print would raise, and from
    outside the per-track error handling, so the whole run would end over a
    track that downloaded perfectly well.
    """
    monkeypatch.setattr(YTPL.sys, 'stdout', _Console('cp866'))

    assert YTPL._console_safe('Taeha (태하) Dive in Love') == 'Taeha (??) Dive in Love'


def test_console_safe_leaves_text_it_can_encode(monkeypatch):
    monkeypatch.setattr(YTPL.sys, 'stdout', _Console('utf-8'))

    assert YTPL._console_safe('Taeha (태하)') == 'Taeha (태하)'


def test_console_safe_without_a_known_encoding(monkeypatch):
    monkeypatch.setattr(YTPL.sys, 'stdout', _Console(None))

    assert YTPL._console_safe('plain ascii') == 'plain ascii'


def test_console_safe_with_a_nonsense_encoding(monkeypatch):
    monkeypatch.setattr(YTPL.sys, 'stdout', _Console('not-an-encoding'))

    assert YTPL._console_safe('Taeha (태하)') == 'Taeha (??)'


def test_status_line_survives_an_unencodable_title(monkeypatch):
    """
    The status line and the per-track line both go through the console, so
    both have to cope with a title it cannot represent.
    """
    console = _Console('cp866')
    monkeypatch.setattr(YTPL.sys, 'stdout', console)

    YTPL._status('[64/426] Taeha (태하)')
    YTPL._line('[64/426] Taeha (태하) -> 1 file')

    assert '??' in console.text
    assert '태' not in console.text


# --- text the system code page cannot decode -------------------------------

def test_subprocess_output_is_decoded_as_utf8():
    assert YTPL.SUBPROCESS_TEXT['encoding'] == 'utf-8'
    assert YTPL.SUBPROCESS_TEXT['errors'] == 'replace'


def test_ffmpeg_output_with_a_korean_title_is_readable(tmp_path):
    """
    The real failure: ffmpeg echoes the title it finds, and decoding that with
    the system code page raised inside subprocess's reader thread. Losing that
    output is not cosmetic - silencedetect's findings are parsed out of it.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    media = tmp_path / 'korean.mp3'
    subprocess.run([ffmpeg, '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                    '-metadata', "title=Taeha (태하)", '-y', str(media)],
                   capture_output=True, check=True)

    result = subprocess.run([ffmpeg, '-i', str(media), '-f', 'null', '-'],
                            **YTPL.SUBPROCESS_TEXT)

    assert 'Taeha' in result.stderr


def test_silence_detection_reads_output_of_a_file_with_a_korean_title(tmp_path):
    """
    The same file, through the function that actually parses ffmpeg's output.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    media = tmp_path / 'korean.wav'
    # Two seconds of tone, two of silence, two of tone.
    parts = []
    for i, src in enumerate(('sine=frequency=440:duration=2:sample_rate=8000',
                             'anullsrc=r=8000:cl=mono',
                             'sine=frequency=660:duration=2:sample_rate=8000')):
        piece = tmp_path / f'{i}.wav'
        subprocess.run([ffmpeg, '-f', 'lavfi', '-i', src, '-t', '2',
                        '-ar', '8000', '-ac', '1', '-y', str(piece)],
                       capture_output=True, check=True)
        parts.append(piece)
    listing = tmp_path / 'list.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in parts), encoding='utf-8')
    subprocess.run([ffmpeg, '-f', 'concat', '-safe', '0', '-i', str(listing),
                    '-metadata', "title=Taeha (태하)",
                    '-ar', '8000', '-ac', '1', '-y', str(media)],
                   capture_output=True, check=True)

    pauses = YTPL._run_silencedetect(str(media), '-30dB', 0.8)

    assert len(pauses) == 1                   # the gap was found, output intact
