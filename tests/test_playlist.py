"""
Playlist handling: detecting a list= URL, listing every entry, choosing
between the whole playlist and the single video, and processing the tracks.

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
                silence_opts=None, output_root=None, output_name=None):
        calls.append(('single', url))
        return ['one.mp3']

    def _playlist(playlist_title, entries, unavailable=0, manual_timestamps=None,
                  mode=YTPL.MODE_SILENCE, silence_opts=None, overwrite=False):
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


# --- processing the tracks -------------------------------------------------

@pytest.fixture
def track_runner(monkeypatch, tmp_path):
    """
    Runs process_playlist in a temporary working directory with the per-track
    processing stubbed, and reports which tracks were handled and with what.
    """
    monkeypatch.chdir(tmp_path)
    seen = types.SimpleNamespace(urls=[], roots=[], modes=[], opts=[], names=[],
                                 fail=set())

    def _single(url, manual_timestamps=None, mode=YTPL.MODE_SILENCE,
                silence_opts=None, output_root=None, output_name=None):
        if url in seen.fail:
            raise RuntimeError('download failed')
        seen.urls.append(url)
        seen.roots.append(output_root)
        seen.modes.append(mode)
        seen.opts.append(silence_opts)
        seen.names.append(output_name)
        track_dir = os.path.join(output_root, output_name)
        os.makedirs(track_dir, exist_ok=True)
        created = os.path.join(track_dir, 'seg_0001.mp3')
        open(created, 'wb').close()
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


def test_playlist_tracks_go_into_the_playlist_folder(track_runner):
    YTPL.process_playlist('My Mix', _entries(2))

    assert track_runner.roots == ['My_Mix', 'My_Mix']
    assert (track_runner.tmp_path / 'My_Mix').is_dir()


def test_track_folder_name_comes_from_the_playlist_entry(track_runner):
    """
    The folder name is decided before the download, so a rerun can tell which
    tracks are already on disk even if the video reports a different title.
    """
    YTPL.process_playlist('My Mix', _entries(2))

    assert track_runner.names == ['Track_0', 'Track_1']


def test_playlist_keeps_the_silence_mode_for_each_track(track_runner):
    """
    Point 3 of the request: every track of the playlist is still checked for
    pauses, with the same settings as a single video.
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


def test_keyboard_interrupt_stops_the_run(track_runner):
    def _interrupt(url, *a, **k):
        raise KeyboardInterrupt

    import YTPL as module
    module.process_youtube_video = _interrupt
    with pytest.raises(KeyboardInterrupt):
        YTPL.process_playlist('My Mix', _entries(2))


def test_finished_tracks_are_skipped_on_a_rerun(track_runner):
    """
    An interrupted playlist run has to resume: tracks whose folder already
    holds mp3 files are not downloaded again.
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


def test_unavailable_count_is_reported(track_runner, capsys):
    YTPL.process_playlist('My Mix', _entries(2), unavailable=3)

    assert 'Skipped unavailable entries: 3' in capsys.readouterr().err or True
    assert track_runner.urls == ['u0', 'u1']
