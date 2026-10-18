"""
Audio splitting: timestamp validation and real cutting with ffmpeg.
"""
import os
import subprocess

import imageio_ffmpeg
import pytest

import YTPL


def _duration(path):
    """
    File duration according to ffmpeg (read from stderr, as YTPL itself does).
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([ffmpeg, '-i', path, '-f', 'null', '-'],
                            capture_output=True, text=True)
    marker = result.stderr.rsplit('time=', 1)[1][:11]
    h, m, s = marker.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_split_creates_segments(album_mp3, tmp_path):
    path, duration = album_mp3
    created = YTPL.split_audio(path, [41.0, 83.0], duration,
                               base_name='album', output_dir=str(tmp_path))

    assert len(created) == 3
    assert [os.path.basename(p) for p in created] == [
        'album_0001.mp3', 'album_0002.mp3', 'album_0003.mp3',
    ]
    assert all(os.path.getsize(p) > 0 for p in created)


def test_segment_durations_match_cuts(album_mp3, tmp_path):
    path, duration = album_mp3
    created = YTPL.split_audio(path, [41.0, 83.0], duration,
                               base_name='album', output_dir=str(tmp_path))

    for segment, expected in zip(created, (41.0, 42.0, duration - 83.0)):
        assert _duration(segment) == pytest.approx(expected, abs=1.5)


def test_no_timestamps_gives_single_segment(album_mp3, tmp_path):
    path, duration = album_mp3
    created = YTPL.split_audio(path, [], duration,
                               base_name='whole', output_dir=str(tmp_path))

    assert len(created) == 1
    assert _duration(created[0]) == pytest.approx(duration, abs=1.5)


def test_timestamps_beyond_duration_are_skipped(album_mp3, tmp_path):
    path, duration = album_mp3
    created = YTPL.split_audio(path, [41.0, duration + 500], duration,
                               base_name='album', output_dir=str(tmp_path))

    # The timestamp past the end of the video is dropped: the 41 s cut remains.
    assert len(created) == 2


def test_non_positive_timestamps_are_skipped(album_mp3, tmp_path):
    path, duration = album_mp3
    created = YTPL.split_audio(path, [0, -10, 41.0], duration,
                               base_name='album', output_dir=str(tmp_path))

    assert len(created) == 2
