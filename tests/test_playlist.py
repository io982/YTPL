"""
Playlist handling: detecting a list= URL, listing every entry, choosing
between the whole playlist and the single video, processing the tracks into one
folder, and stopping a long run.

The playlist listing is stubbed, so these tests never reach YouTube. The shape
of the stub matches what yt-dlp really returns for a flat extraction, including
the empty stubs that deleted and private videos come back as.
"""
import os
import types

import pytest

import YTPL

PLAYLIST_URL = ('https://www.youtube.com/watch?v=eUzwMZf0TXg'
                '&list=PLyIFQr1wryPKU_QHC9NxCf7LuFAQtTLf0&index=1')
PLAIN_URL = 'https://www.youtube.com/watch?v=yaj8rZsKZPw'


def _flat_playlist(count=5, unavailable=0, title='Essential NewRetroWave Vocal Jams'):
    """
    Build what yt-dlp returns for extract_flat on a playlist.
    """
    entries = [{'id': f'vid{i:03d}', 'title': f'Artist {i} - Track {i}',
                'url': f'https://www.youtube.com/watch?v=vid{i:03d}'}
               for i in range(count)]
    # Deleted/private videos: stubs with neither title nor duration.
    entries += [{'id': f'gone{i}', 'title': None} for i in range(unavailable)]
    return {'_type': 'playlist', 'title': title, 'playlist_count': len(entries),
            'entries': entries}


@pytest.fixture
def stub_listing(monkeypatch):
    """
    Replaces the yt-dlp call inside fetch_playlist_entries with a canned
    listing, and records the URL it was asked about.
    """
    seen = types.SimpleNamespace(url=None)

    def _install(payload):
        class FakeYDL:
            def __init__(self, opts):
                seen.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                seen.url = url
                return payload

        monkeypatch.setattr(YTPL.yt_dlp, 'YoutubeDL', FakeYDL)
        return seen

    return _install


# --- URL recognition -------------------------------------------------------

@pytest.mark.parametrize('url, expected', [
    (PLAYLIST_URL, 'PLyIFQr1wryPKU_QHC9NxCf7LuFAQtTLf0'),
    ('https://www.youtube.com/playlist?list=PLabc123', 'PLabc123'),
    (PLAIN_URL, None),
    ('https://youtu.be/yaj8rZsKZPw', None),
    ('https://www.youtube.com/watch?v=x&list=', None),
    ('not a url at all', None),
])
def test_extract_playlist_id(url, expected):
    assert YTPL.extract_playlist_id(url) == expected


# --- listing every entry ---------------------------------------------------

def test_fetch_playlist_entries_lists_everything(stub_listing):
    """
    yt-dlp walks the playlist pagination itself, so a listing far longer than
    the ~200 items the web page shows at first comes back whole.
    """
    stub_listing(_flat_playlist(count=429))
    title, entries, unavailable = YTPL.fetch_playlist_entries(PLAYLIST_URL)

    assert title == 'Essential NewRetroWave Vocal Jams'
    assert len(entries) == 429
    assert unavailable == 0
    assert entries[0]['url'].endswith('vid000')
    assert entries[-1]['url'].endswith('vid428')


def test_fetch_playlist_entries_skips_unavailable(stub_listing):
    stub_listing(_flat_playlist(count=426, unavailable=3))
    _, entries, unavailable = YTPL.fetch_playlist_entries(PLAYLIST_URL)

    assert len(entries) == 426
    assert unavailable == 3
    assert all(e['title'] for e in entries)


def test_fetch_playlist_entries_uses_flat_extraction(stub_listing):
    """
    The listing must stay flat: pulling full metadata for hundreds of videos
    would take minutes instead of seconds.
    """
    seen = stub_listing(_flat_playlist())
    YTPL.fetch_playlist_entries(PLAYLIST_URL)

    assert seen.opts['extract_flat'] == 'in_playlist'
    assert seen.opts['skip_download'] is True
    assert seen.url == PLAYLIST_URL


def test_fetch_playlist_entries_builds_url_when_missing(stub_listing):
    stub_listing({'_type': 'playlist', 'title': 'P',
                  'entries': [{'id': 'abc123', 'title': 'Track'}]})
    _, entries, _ = YTPL.fetch_playlist_entries(PLAYLIST_URL)

    assert entries[0]['url'] == 'https://www.youtube.com/watch?v=abc123'


def test_fetch_playlist_entries_on_a_single_video(stub_listing):
    stub_listing({'_type': 'video', 'title': 'Just a video', 'duration': 100})
    assert YTPL.fetch_playlist_entries(PLAIN_URL) == (None, [], 0)


# --- choosing between the playlist and the single video --------------------

@pytest.fixture
def spy_process(monkeypatch):
    """
    Replaces both processing functions so only the routing is observed.
    """
    calls = []

    def _single(url, manual_timestamps=None, mode=YTPL.MODE_SILENCE,
                silence_opts=None, output_dir=None, base_name=None):
        calls.append(('single', url))
        return ['one.mp3']

    def _playlist(playlist_title, entries, unavailable=0, manual_timestamps=None,
                  mode=YTPL.MODE_SILENCE, silence_opts=None, overwrite=False,
                  max_failures=YTPL.DEFAULT_MAX_FAILURES,
                  attempts=YTPL.DEFAULT_TRACK_ATTEMPTS):
        calls.append(('playlist', playlist_title, len(entries)))
        return ['many.mp3']

    monkeypatch.setattr(YTPL, 'process_youtube_video', _single)
    monkeypatch.setattr(YTPL, 'process_playlist', _playlist)
    return calls


def test_plain_url_never_lists_a_playlist(spy_process, monkeypatch):
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries',
                        lambda url: pytest.fail('no listing for a plain URL'))
    YTPL.process_url(PLAIN_URL)

    assert spy_process == [('single', PLAIN_URL)]


def test_no_playlist_choice_skips_the_listing(spy_process, monkeypatch):
    """
    --no-playlist must not even list the playlist: that is a network round trip
    for an answer already given.
    """
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries',
                        lambda url: pytest.fail('listing not needed'))
    YTPL.process_url(PLAYLIST_URL, playlist_choice=False)

    assert spy_process == [('single', PLAYLIST_URL)]


def test_playlist_choice_processes_every_track(spy_process, monkeypatch):
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries',
                        lambda url: ('Mix', [{'id': 'a', 'title': 'A', 'url': 'u1'},
                                             {'id': 'b', 'title': 'B', 'url': 'u2'}], 0))
    YTPL.process_url(PLAYLIST_URL, playlist_choice=True)

    assert spy_process == [('playlist', 'Mix', 2)]


def test_non_interactive_run_takes_the_single_video(spy_process, monkeypatch):
    """
    A few hundred tracks must never start downloading unattended: without an
    explicit --playlist, a non-interactive run takes just the one video.
    """
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries',
                        lambda url: ('Mix', [{'id': 'a', 'title': 'A', 'url': 'u1'}] * 429, 0))
    YTPL.process_url(PLAYLIST_URL, interactive=False)

    assert spy_process == [('single', PLAYLIST_URL)]


@pytest.mark.parametrize('answer, expect_playlist', [
    ('', True),
    ('y', True),
    ('o', False),
    ('O', False),
    ('one', False),
    ('single', False),
    ('n', False),
])
def test_interactive_playlist_prompt(spy_process, monkeypatch, answer, expect_playlist):
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries',
                        lambda url: ('Mix', [{'id': 'a', 'title': 'A', 'url': 'u1'}], 0))
    monkeypatch.setattr('builtins.input', lambda *a: answer)
    YTPL.process_url(PLAYLIST_URL, interactive=True)

    assert spy_process[0][0] == ('playlist' if expect_playlist else 'single')


def test_unlistable_playlist_falls_back_to_the_video(spy_process, monkeypatch):
    monkeypatch.setattr(YTPL, 'fetch_playlist_entries', lambda url: (None, [], 0))
    YTPL.process_url(PLAYLIST_URL, playlist_choice=True)

    assert spy_process == [('single', PLAYLIST_URL)]


def test_parser_playlist_flags():
    parser = YTPL.build_parser()
    assert parser.parse_args([]).playlist is None            # ask
    assert parser.parse_args(['--playlist']).playlist is True
    assert parser.parse_args(['--no-playlist']).playlist is False
    assert parser.parse_args([]).overwrite is False
    assert parser.parse_args(['--overwrite']).overwrite is True


# --- recognising finished tracks ------------------------------------------

def test_track_is_done(tmp_path):
    open(tmp_path / 'Song_0001.mp3', 'wb').close()

    assert YTPL.track_is_done(str(tmp_path), 'Song') is True
    assert YTPL.track_is_done(str(tmp_path), 'Other') is False


def test_track_is_done_does_not_confuse_a_longer_name(tmp_path):
    """
    All tracks share one folder, so matching on a prefix would report 'Song' as
    finished when only 'Song_Extended' is there.
    """
    open(tmp_path / 'Song_Extended_0001.mp3', 'wb').close()

    assert YTPL.track_is_done(str(tmp_path), 'Song_Extended') is True
    assert YTPL.track_is_done(str(tmp_path), 'Song') is False


def test_track_is_done_ignores_other_files(tmp_path):
    open(tmp_path / 'Song.txt', 'wb').close()
    open(tmp_path / 'Song_abcd.mp3', 'wb').close()

    assert YTPL.track_is_done(str(tmp_path), 'Song') is False


def test_track_is_done_on_a_missing_folder(tmp_path):
    assert YTPL.track_is_done(str(tmp_path / 'nope'), 'Song') is False


def test_remove_track_files(tmp_path):
    for name in ('Song_0001.mp3', 'Song_0002.mp3', 'Other_0001.mp3', 'Song.txt'):
        open(tmp_path / name, 'wb').close()

    YTPL.remove_track_files(str(tmp_path), 'Song')

    assert sorted(os.listdir(tmp_path)) == ['Other_0001.mp3', 'Song.txt']


# --- processing the tracks -------------------------------------------------

@pytest.fixture
def track_runner(monkeypatch, tmp_path):
    """
    Runs process_playlist in a temporary working directory with the per-track
    processing stubbed, and reports which tracks were handled and with what.
    """
    monkeypatch.chdir(tmp_path)
    # Retries are real seconds in a real run; here they only slow the suite.
    monkeypatch.setattr(YTPL, 'TRACK_RETRY_WAIT', 0)
    seen = types.SimpleNamespace(urls=[], dirs=[], names=[], modes=[], opts=[],
                                 fail=set(), interrupt=set(), attempts=[])

    def _single(url, manual_timestamps=None, mode=YTPL.MODE_SILENCE,
                silence_opts=None, output_dir=None, base_name=None):
        seen.attempts.append(url)
        if url in seen.fail:
            raise RuntimeError('download failed')
        seen.urls.append(url)
        seen.dirs.append(output_dir)
        seen.names.append(base_name)
        seen.modes.append(mode)
        seen.opts.append(silence_opts)
        os.makedirs(output_dir, exist_ok=True)
        created = os.path.join(output_dir, f'{base_name}_0001.mp3')
        open(created, 'wb').close()
        if url in seen.interrupt:
            # Interrupted after writing a segment, as a real run would be.
            raise KeyboardInterrupt
        return [created]

    monkeypatch.setattr(YTPL, 'process_youtube_video', _single)
    seen.tmp_path = tmp_path
    return seen


def _entries(n):
    return [{'id': f'v{i}', 'title': f'Track {i}', 'url': f'u{i}'} for i in range(n)]


def test_playlist_processes_every_track(track_runner):
    created = YTPL.process_playlist('My Mix', _entries(4))

    assert track_runner.urls == ['u0', 'u1', 'u2', 'u3']
    assert len(created) == 4


def test_all_tracks_land_in_one_folder(track_runner):
    """
    One folder for the whole playlist, not a folder per track.
    """
    YTPL.process_playlist('My Mix', _entries(3))

    assert track_runner.dirs == ['My_Mix'] * 3
    folder = track_runner.tmp_path / 'My_Mix'
    assert sorted(os.listdir(folder)) == ['Track_0_0001.mp3',
                                          'Track_1_0001.mp3',
                                          'Track_2_0001.mp3']
    assert [p for p in folder.iterdir() if p.is_dir()] == []


def test_file_names_come_from_the_playlist_entries(track_runner):
    """
    The name is decided before the download, so a rerun can tell which tracks
    are already on disk even if the video reports a different title.
    """
    YTPL.process_playlist('My Mix', _entries(2))

    assert track_runner.names == ['Track_0', 'Track_1']


def test_playlist_keeps_the_silence_mode_for_each_track(track_runner):
    """
    Every track of the playlist is still checked for pauses, with the same
    settings as a single video.
    """
    opts = {'min_segment': 20.0}
    YTPL.process_playlist('My Mix', _entries(3), mode=YTPL.MODE_SILENCE, silence_opts=opts)

    assert track_runner.modes == [YTPL.MODE_SILENCE] * 3
    assert track_runner.opts == [opts] * 3


def test_one_failing_track_does_not_stop_the_run(track_runner, capsys):
    track_runner.fail = {'u1'}
    created = YTPL.process_playlist('My Mix', _entries(4))

    assert track_runner.urls == ['u0', 'u2', 'u3']
    assert len(created) == 3
    out = capsys.readouterr().out
    assert 'Failed tracks: 1' in out
    assert 'Track 1' in out


def test_finished_tracks_are_skipped_on_a_rerun(track_runner):
    """
    An interrupted playlist run has to resume: tracks already on disk are not
    downloaded again.
    """
    entries = _entries(3)
    YTPL.process_playlist('My Mix', entries)
    track_runner.urls.clear()

    YTPL.process_playlist('My Mix', entries)

    assert track_runner.urls == []


def test_overwrite_redownloads_finished_tracks(track_runner):
    entries = _entries(2)
    YTPL.process_playlist('My Mix', entries)
    track_runner.urls.clear()

    YTPL.process_playlist('My Mix', entries, overwrite=True)

    assert track_runner.urls == ['u0', 'u1']


def test_unavailable_count_is_reported(track_runner, caplog):
    YTPL.process_playlist('My Mix', _entries(2), unavailable=3)

    assert 'Skipped unavailable entries: 3' in caplog.text
    assert track_runner.urls == ['u0', 'u1']


# --- stopping a long run ---------------------------------------------------

def test_ctrl_c_stops_the_run_without_a_traceback(track_runner, capsys):
    """
    Ctrl+C part way through a playlist ends the run tidily, with a summary and
    how to carry on, rather than a traceback.
    """
    track_runner.interrupt = {'u1'}
    created = YTPL.process_playlist('My Mix', _entries(4))

    assert track_runner.urls == ['u0', 'u1']      # stopped, u2/u3 untouched
    assert len(created) == 1                       # only the finished track
    out = capsys.readouterr().out
    assert 'Playlist stopped' in out
    assert 'Stopped at track 2 of 4' in out
    assert 'Run the same command again' in out


def test_interrupted_track_is_rolled_back(track_runner):
    """
    The track in progress is incomplete, so its files are removed: otherwise
    the next run would treat it as finished and skip it.
    """
    track_runner.interrupt = {'u1'}
    YTPL.process_playlist('My Mix', _entries(4))

    folder = track_runner.tmp_path / 'My_Mix'
    assert os.listdir(folder) == ['Track_0_0001.mp3']


def test_rerun_after_ctrl_c_carries_on(track_runner):
    entries = _entries(4)
    track_runner.interrupt = {'u1'}
    YTPL.process_playlist('My Mix', entries)

    track_runner.interrupt = set()
    track_runner.urls.clear()
    YTPL.process_playlist('My Mix', entries)

    # Track 0 was finished, so it is skipped; the rest are picked up.
    assert track_runner.urls == ['u1', 'u2', 'u3']


def test_stop_file_stops_the_run(track_runner, capsys):
    """
    A file named STOP halts the run at the next track boundary, for when the
    console cannot be reached to press Ctrl+C.
    """
    open(track_runner.tmp_path / YTPL.STOP_FILE, 'wb').close()

    created = YTPL.process_playlist('My Mix', _entries(3))

    assert track_runner.urls == []
    assert created == []
    out = capsys.readouterr().out
    assert 'Playlist stopped' in out
    assert 'Stopped at track 1 of 3' in out


def test_stop_file_is_noticed_between_tracks(track_runner):
    """
    The file is checked before each track, so one created while a track is
    downloading takes effect as soon as that track is finished.
    """
    entries = _entries(4)
    original = YTPL.process_youtube_video

    def _single(url, *args, **kwargs):
        result = original(url, *args, **kwargs)
        if url == 'u0':
            open(track_runner.tmp_path / YTPL.STOP_FILE, 'wb').close()
        return result

    YTPL.process_youtube_video = _single
    try:
        YTPL.process_playlist('My Mix', entries)
    finally:
        YTPL.process_youtube_video = original

    assert track_runner.urls == ['u0']


def test_stop_requested_checks_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert YTPL.stop_requested() is False

    open(tmp_path / YTPL.STOP_FILE, 'wb').close()
    assert YTPL.stop_requested() is True

# --- the failure ceiling ---------------------------------------------------

def test_run_gives_up_after_failures_in_a_row(track_runner, capsys):
    """
    Once downloads stop working they stay broken, so a run must not put
    several hundred tracks through the same failure.
    """
    entries = _entries(20)
    track_runner.fail = {f'u{i}' for i in range(20)}

    YTPL.process_playlist('My Mix', entries, max_failures=3)

    assert track_runner.urls == []
    out = capsys.readouterr().out
    assert '3 failures in a row' in out
    assert 'Stopped at track 3 of 20' in out


def test_a_success_resets_the_failure_count(track_runner):
    entries = _entries(8)
    track_runner.fail = {'u0', 'u1', 'u3', 'u4'}

    YTPL.process_playlist('My Mix', entries, max_failures=3)

    # Two failures, then u2 succeeds and resets, so the run reaches the end.
    assert track_runner.urls == ['u2', 'u5', 'u6', 'u7']


def test_max_failures_zero_never_gives_up(track_runner, capsys):
    entries = _entries(6)
    track_runner.fail = {f'u{i}' for i in range(6)}

    YTPL.process_playlist('My Mix', entries, max_failures=0)

    out = capsys.readouterr().out
    assert 'Failed tracks: 6' in out          # every track was attempted
    assert 'Stopped at track' not in out


def test_parser_max_failures():
    assert YTPL.build_parser().parse_args([]).max_failures == YTPL.DEFAULT_MAX_FAILURES
    assert YTPL.build_parser().parse_args(['--max-failures', '0']).max_failures == 0


# --- migrating the old per-track folders ----------------------------------

def test_existing_per_track_folders_are_flattened(tmp_path):
    """
    Earlier versions wrote a folder per track. The files move up, or a rerun
    would not recognise them and would download everything again.
    """
    root = tmp_path / 'Mix'
    (root / 'Track_0').mkdir(parents=True)
    (root / 'Track_1').mkdir()
    open(root / 'Track_0' / 'Track_0_0001.mp3', 'wb').close()
    open(root / 'Track_0' / 'Track_0_0002.mp3', 'wb').close()
    open(root / 'Track_1' / 'Track_1_0001.mp3', 'wb').close()

    moved = YTPL.flatten_playlist_folder(str(root))

    assert moved == 3
    assert sorted(os.listdir(root)) == ['Track_0_0001.mp3', 'Track_0_0002.mp3',
                                        'Track_1_0001.mp3']


def test_flattening_leaves_other_files_alone(tmp_path):
    root = tmp_path / 'Mix'
    (root / 'Track_0').mkdir(parents=True)
    open(root / 'Track_0' / 'cover.jpg', 'wb').close()
    open(root / 'Track_0' / 'Track_0_0001.mp3', 'wb').close()

    YTPL.flatten_playlist_folder(str(root))

    # The mp3 moved up; the subfolder stays because something is still in it.
    assert 'Track_0_0001.mp3' in os.listdir(root)
    assert os.listdir(root / 'Track_0') == ['cover.jpg']


def test_flattening_does_not_overwrite(tmp_path):
    root = tmp_path / 'Mix'
    (root / 'Track_0').mkdir(parents=True)
    open(root / 'Track_0_0001.mp3', 'wb').close()
    open(root / 'Track_0' / 'Track_0_0001.mp3', 'wb').close()

    moved = YTPL.flatten_playlist_folder(str(root))

    assert moved == 0
    assert os.path.exists(root / 'Track_0' / 'Track_0_0001.mp3')


def test_old_layout_is_picked_up_as_done(track_runner):
    """
    The 15 files an earlier run had left in per-track folders must count as
    done once they have been moved up.
    """
    root = track_runner.tmp_path / 'My_Mix'
    (root / 'Track_0').mkdir(parents=True)
    open(root / 'Track_0' / 'Track_0_0001.mp3', 'wb').close()

    YTPL.process_playlist('My Mix', _entries(2))

    assert track_runner.urls == ['u1']        # Track 0 recognised, not redone


# --- pause and stop keys ---------------------------------------------------

@pytest.mark.parametrize('key', ['s', 'q'])
def test_stop_keys(monkeypatch, tmp_path, key):
    """
    's' and 'q' both stop: 'q' for quit is what one reaches for.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(YTPL, '_read_key', lambda: key)

    assert YTPL.check_controls() == 'stop'


def test_no_key_carries_on(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(YTPL, '_read_key', lambda: None)

    assert YTPL.check_controls() is None


def test_pause_waits_for_a_second_p(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    keys = iter(['p', None, None, 'p'])
    monkeypatch.setattr(YTPL, '_read_key', lambda: next(keys, None))
    monkeypatch.setattr(YTPL.time, 'sleep', lambda s: None)

    assert YTPL.check_controls() is None
    out = capsys.readouterr().out
    assert 'paused' in out
    assert 'carrying on' in out


def test_pause_can_be_stopped(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    keys = iter(['p', None, 's'])
    monkeypatch.setattr(YTPL, '_read_key', lambda: next(keys, None))
    monkeypatch.setattr(YTPL.time, 'sleep', lambda s: None)

    assert YTPL.check_controls() == 'stop'


def test_read_key_is_safe_without_a_console():
    """
    Output redirected to a file, or a platform without msvcrt: it must return
    quietly rather than raise, and never hand back anything but one character.
    """
    key = YTPL._read_key()

    assert key is None or (isinstance(key, str) and len(key) == 1)


# --- the quiet console ----------------------------------------------------

def test_console_shows_one_line_per_track(track_runner, capsys):
    YTPL.process_playlist('My Mix', _entries(2))
    out = capsys.readouterr().out

    assert 'Track 0 -> 1 file' in out
    assert 'Track 1 -> 1 file' in out
    assert 'Keys: [p] pause' in out
    assert '2 tracks -> My_Mix/' in out


def test_console_shows_the_failure_reason(track_runner, capsys):
    track_runner.fail = {'u0'}
    YTPL.process_playlist('My Mix', _entries(2))

    assert 'FAILED: download failed' in capsys.readouterr().out


# --- retrying a track ------------------------------------------------------

def test_a_track_is_retried_before_being_given_up_on(track_runner):
    """
    YouTube hands out a different player client each extraction and some of
    them answer 403, so the same track is worth another go.
    """
    track_runner.fail = {'u0'}

    YTPL.process_playlist('My Mix', _entries(1), attempts=3)

    assert track_runner.attempts == ['u0', 'u0', 'u0']


def test_a_retry_that_works_counts_as_a_success(track_runner, capsys):
    entries = _entries(1)
    calls = {'n': 0}
    original = YTPL.process_youtube_video

    def _flaky(url, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('403 on the first client')
        return original(url, *args, **kwargs)

    YTPL.process_youtube_video = _flaky
    try:
        created = YTPL.process_playlist('My Mix', entries, attempts=3)
    finally:
        YTPL.process_youtube_video = original

    assert len(created) == 1
    out = capsys.readouterr().out
    assert 'Track 0 -> 1 file' in out
    assert 'FAILED' not in out


def test_a_failed_attempt_leaves_nothing_behind(track_runner):
    """
    Half a track on disk would be taken for a finished one by the next run.
    """
    entries = _entries(1)
    original = YTPL.process_youtube_video

    def _writes_then_fails(url, *args, **kwargs):
        original(url, *args, **kwargs)        # writes the segment
        raise RuntimeError('died after writing')

    YTPL.process_youtube_video = _writes_then_fails
    try:
        YTPL.process_playlist('My Mix', entries, attempts=2)
    finally:
        YTPL.process_youtube_video = original

    assert os.listdir(track_runner.tmp_path / 'My_Mix') == []


def test_parser_attempts():
    assert YTPL.build_parser().parse_args([]).attempts == YTPL.DEFAULT_TRACK_ATTEMPTS
    assert YTPL.build_parser().parse_args(['--attempts', '1']).attempts == 1


def test_the_403_hint_is_said_once_per_run(track_runner, caplog, monkeypatch):
    """
    A 403 a retry gets past needs no advice, and four hundred of them need it
    once: it is said when a track is given up on, and then not again.
    """
    monkeypatch.setattr(YTPL, '_403_hint_shown', False)
    track_runner.fail = {'u0', 'u1'}

    YTPL.process_playlist('My Mix', _entries(2), attempts=1,
                          max_failures=0)

    assert caplog.text.count('pip install yt-dlp-ejs') == 0   # not a 403 here


def test_a_final_403_explains_itself(track_runner, caplog, monkeypatch):
    monkeypatch.setattr(YTPL, '_403_hint_shown', False)
    original = YTPL.process_youtube_video

    def _forbidden(url, *args, **kwargs):
        raise RuntimeError('HTTP Error 403: Forbidden')

    YTPL.process_youtube_video = _forbidden
    try:
        YTPL.process_playlist('My Mix', _entries(3), attempts=1, max_failures=0)
    finally:
        YTPL.process_youtube_video = original

    # Three tracks gave up on a 403; the advice is printed for the first only.
    assert caplog.text.count('pip install yt-dlp-ejs') == 1
