"""
Silence analysis: detect_silence_timestamps and its helpers.

These tests run against the real ffmpeg and locally generated audio, so they
cover the whole path: silencedetect -> candidate selection -> threshold recursion.
"""
import pytest

import YTPL


def test_enforce_min_segment_drops_short_pieces():
    # Cuts at 10 s and 12 s leave stubs shorter than 15 s: only the last survives.
    assert YTPL._enforce_min_segment([10.0, 12.0, 40.0], total=100.0, min_seg=15.0) == [40.0]


def test_enforce_min_segment_drops_short_tail():
    # The last segment (100 - 95 = 5 s) is too short, so that cut is discarded.
    assert YTPL._enforce_min_segment([30.0, 95.0], total=100.0, min_seg=15.0) == [30.0]


def test_enforce_min_segment_keeps_valid_cuts():
    assert YTPL._enforce_min_segment([30.0, 60.0], total=100.0, min_seg=15.0) == [30.0, 60.0]


def test_enforce_min_segment_without_cuts():
    assert YTPL._enforce_min_segment([], total=100.0, min_seg=15.0) == []


def test_short_album_is_split_by_gaps(album_audio):
    """
    The key check for short audio: 124 s is less than max_segment_len (300 s),
    but the pauses between tracks are obvious, so it still has to be split.
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration)

    assert len(cuts) == 2
    assert cuts[0] == pytest.approx(41.0, abs=1.0)
    assert cuts[1] == pytest.approx(83.0, abs=1.0)


def test_cuts_land_in_the_middle_of_a_gap(album_audio):
    """
    A cut must fall inside the pause rather than into an audible piece,
    otherwise a track loses its ending or picks up the start of the next one.
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration)

    for cut, gap_start in zip(cuts, (40.0, 82.0)):
        assert gap_start < cut < gap_start + 2.0


def test_no_cuts_outside_duration(album_audio):
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration)

    assert all(0 < c < duration for c in cuts)
    assert cuts == sorted(cuts)
    assert len(set(cuts)) == len(cuts)


def test_min_segment_merges_tiny_tracks(album_audio):
    """
    With min_segment above the track length the extra cuts disappear: no cut
    yields two 60 s segments (the tracks are only 40 s), so the file is left
    whole instead of becoming a pile of stubs.
    """
    path, duration = album_audio

    assert YTPL.detect_silence_timestamps(path, duration, min_segment=60.0) == []
    # Control: with the default threshold this very file does get cut.
    assert len(YTPL.detect_silence_timestamps(path, duration)) == 2


def test_gapless_audio_is_hard_split(gapless_audio):
    """
    700 s without pauses: no threshold fires, so the piece is split hard into
    equal parts no longer than max_segment_len.
    """
    path, duration = gapless_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=300.0)

    bounds = [0.0] + cuts + [duration]
    lengths = [b - a for a, b in zip(bounds, bounds[1:])]

    assert len(cuts) == 2  # ceil(700 / 300) = 3 parts
    assert max(lengths) <= 300.0 + 1e-6
    assert max(lengths) - min(lengths) < 1e-6  # parts of equal length


def test_threshold_is_lowered_only_for_long_segments(mixed_audio):
    """
    The 1.0 s pause is shorter than the starting threshold (1.5 s), so only the
    2.0 s pause shows up in the first pass. The remaining ~201 s piece does not
    exceed max_segment_len=300, so the threshold is never lowered and the short
    pause is not cut.
    """
    path, duration = mixed_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=300.0)

    assert len(cuts) == 1
    assert cuts[0] == pytest.approx(201.0, abs=1.5)


def test_long_segment_triggers_lower_threshold(mixed_audio):
    """
    The same file with max_segment_len=150: the piece before the long pause
    (~201 s) counts as too long, the threshold drops to 0.8 s and the short
    pause (1.0 s) becomes a cut point as well.
    """
    path, duration = mixed_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=150.0)

    assert len(cuts) == 2
    assert cuts[0] == pytest.approx(100.5, abs=1.5)
    assert cuts[1] == pytest.approx(201.0, abs=1.5)


def test_strict_noise_threshold_still_finds_real_gaps(album_audio):
    """
    The pauses between tracks are digital silence, so they are detected even
    with a very strict volume threshold (-90 dB).
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, noise_tol='-90dB')

    assert len(cuts) == 2


def test_returns_empty_list_when_nothing_to_cut(gapless_audio):
    """
    No pauses, and no hard split needed (the piece is shorter than
    max_segment_len): the function must return an empty list rather than fail.
    """
    path, duration = gapless_audio
    assert YTPL.detect_silence_timestamps(path, duration,
                                          max_segment_len=duration + 10) == []


def test_min_silence_below_floor_still_works(album_audio):
    """
    A degenerate setting: the starting threshold is below floor. The level list
    collapses to a single value and the function must not fail.
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, min_silence_dur=0.5, floor=0.8)

    assert len(cuts) == 2


def test_default_max_segment_leaves_a_long_song_alone(gapless_audio):
    """
    A 5:28 song with no internal pause was being cut in half simply for being
    longer than the old five-minute limit. Nothing says to cut there, so
    nothing should.
    """
    path, duration = gapless_audio          # 700 s, no pauses at all

    assert YTPL.DEFAULT_MAX_SEGMENT == 900.0
    assert YTPL.detect_silence_timestamps(path, duration) == []


def test_max_segment_zero_never_cuts_where_there_is_no_pause(gapless_audio):
    path, duration = gapless_audio

    assert YTPL.detect_silence_timestamps(path, duration, max_segment_len=0) == []


def test_max_segment_zero_still_cuts_at_real_pauses(album_audio):
    """
    Turning the length rule off must not turn the silence detection off.
    """
    path, duration = album_audio

    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=0)

    assert len(cuts) == 2


def test_default_noise_threshold_ignores_quiet_music():
    """
    A soft musical passage is not a gap between tracks. One 9-minute track had
    seven "pauses" at -30dB and none at -40dB, and was being cut into three.
    """
    assert YTPL.DEFAULT_NOISE == '-40dB'


def test_quiet_passages_are_not_treated_as_gaps(make_audio):
    """
    Tone, then a soft passage, then tone again: music that happens to be
    quiet, not a gap. The old -30dB threshold called it silence.
    """
    path, duration = make_audio([
        ('tone', 30, 440),
        ('quiet', 3),
        ('tone', 30, 660),
    ], 'quiet_passage')

    assert YTPL.detect_silence_timestamps(path, duration, noise_tol='-30dB')
    assert YTPL.detect_silence_timestamps(path, duration) == []
