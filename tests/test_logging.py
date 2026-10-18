"""
Logging: the log file, the environment banner, and the bridge that carries
yt-dlp's own messages into the log.

These tests touch the root logger, so the fixture below puts it back exactly as
it was - otherwise a leftover handler would follow the rest of the suite.
"""
import logging
import os
import time

import pytest

import YTPL


@pytest.fixture
def clean_logging():
    """
    Snapshot of the root logger, restored afterwards.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = [h.level for h in saved_handlers]
    saved_verbose = YTPL._ytdlp_verbose

    yield root

    for handler in list(root.handlers):
        if handler not in saved_handlers:
            handler.close()
            root.removeHandler(handler)
    for handler, level in zip(saved_handlers, saved_levels):
        handler.setLevel(level)
    root.setLevel(saved_level)
    YTPL._ytdlp_verbose = saved_verbose


def test_setup_logging_writes_a_file(clean_logging, tmp_path):
    path = tmp_path / 'run.log'
    assert YTPL.setup_logging(str(path)) == str(path)

    YTPL.logger.info('hello from the test')
    YTPL.logger.debug('debug detail')
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = path.read_text(encoding='utf-8')
    assert 'hello from the test' in text
    # The file always keeps DEBUG, even when the console does not show it.
    assert 'debug detail' in text


def test_setup_logging_keeps_the_console_quiet(clean_logging, tmp_path):
    """
    Without verbose the console is left with warnings and errors only, while
    the file records DEBUG: progress is shown by the status line instead, and
    the running commentary would only bury it.
    """
    YTPL.setup_logging(str(tmp_path / 'run.log'))
    console = clean_logging.handlers[0]
    file_handler = clean_logging.handlers[-1]

    assert console.level == logging.WARNING
    assert file_handler.level == logging.DEBUG
    assert clean_logging.level == logging.DEBUG


def test_verbose_lowers_the_console_level(clean_logging, tmp_path):
    YTPL.setup_logging(str(tmp_path / 'run.log'), verbose=True)

    assert clean_logging.handlers[0].level == logging.DEBUG
    assert YTPL._ytdlp_verbose is True


def test_no_log_file(clean_logging):
    before = len(clean_logging.handlers)

    assert YTPL.setup_logging(None) is None
    assert len(clean_logging.handlers) == before


def test_unwritable_log_file_is_not_fatal(clean_logging, tmp_path):
    """
    A log file that cannot be opened must not stop the run. A missing folder is
    simply created, so the broken case here is a plain file sitting where the
    log folder would have to be.
    """
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a folder', encoding='utf-8')

    assert YTPL.setup_logging(str(blocker / 'run.log')) is None


def test_log_environment_records_the_versions(clean_logging, caplog):
    with caplog.at_level(logging.DEBUG):
        YTPL.log_environment()

    text = caplog.text
    assert YTPL.yt_dlp.version.__version__ in text
    assert 'ffmpeg' in text
    assert 'Python' in text


def test_log_environment_survives_a_broken_ffmpeg(clean_logging, caplog, monkeypatch):
    monkeypatch.setattr(YTPL.imageio_ffmpeg, 'get_ffmpeg_version',
                        lambda: (_ for _ in ()).throw(RuntimeError('no ffmpeg')))
    with caplog.at_level(logging.DEBUG):
        YTPL.log_environment()

    assert 'Could not query ffmpeg' in caplog.text


@pytest.mark.parametrize('method, message, level', [
    ('debug', '[debug] inner detail', 'DEBUG'),
    ('debug', '[youtube] Downloading API JSON', 'INFO'),
    ('info', 'plain info', 'INFO'),
    # yt-dlp's warnings and errors are kept off the console: YTPL reports the
    # outcome of each track itself, with the same text, and the detail is in
    # the log file either way.
    ('warning', 'careful', 'INFO'),
    ('error', 'ERROR: HTTP Error 403: Forbidden', 'INFO'),
])
def test_ytdlp_logger_bridge(caplog, method, message, level):
    """
    yt-dlp sends its screen output through debug() as well, so only the lines
    it marks itself must end up at DEBUG.
    """
    bridge = YTPL._YtdlpLogger()
    with caplog.at_level(logging.DEBUG):
        getattr(bridge, method)(message)

    assert caplog.records[-1].levelname == level
    assert message in caplog.text


def test_ytdlp_problems_are_labelled(caplog):
    """
    Downgraded to INFO, they still have to be findable in the log.
    """
    bridge = YTPL._YtdlpLogger()
    with caplog.at_level(logging.DEBUG):
        bridge.warning('n challenge solving failed')
        bridge.error('HTTP Error 403')

    assert '[yt-dlp warning] n challenge solving failed' in caplog.text
    assert '[yt-dlp error] HTTP Error 403' in caplog.text


def test_download_progress_reports_a_finished_file(caplog):
    with caplog.at_level(logging.INFO):
        YTPL._download_progress({'status': 'finished', 'total_bytes': 5 * 1048576,
                                 'elapsed': 12.4})

    assert 'Downloaded 5.0 MB in 12s' in caplog.text


def test_download_progress_survives_missing_numbers(caplog):
    with caplog.at_level(logging.INFO):
        YTPL._download_progress({'status': 'finished'})

    assert 'Downloaded 0.0 MB' in caplog.text


def test_download_progress_reports_an_error(caplog):
    with caplog.at_level(logging.INFO):
        YTPL._download_progress({'status': 'error', 'filename': 'audio.webm'})

    assert caplog.records[-1].levelname == 'ERROR'
    assert 'audio.webm' in caplog.text


def test_download_progress_ignores_ongoing_updates(caplog):
    """
    The live progress updates must not reach the log: on a long playlist they
    would be thousands of lines.
    """
    with caplog.at_level(logging.DEBUG):
        YTPL._download_progress({'status': 'downloading', 'downloaded_bytes': 1024})

    assert caplog.records == []


def test_parser_logging_flags():
    parser = YTPL.build_parser()

    assert parser.parse_args([]).log_file == YTPL.DEFAULT_LOG_FILE
    assert parser.parse_args(['--no-log-file']).log_file is None
    assert parser.parse_args(['--log-file', 'other.log']).log_file == 'other.log'
    assert parser.parse_args([]).verbose is False
    assert parser.parse_args(['-v']).verbose is True

def test_default_log_file_is_one_per_session(clean_logging, tmp_path, monkeypatch):
    """
    The default name carries the start of the run, so two runs never share a
    file and a log worth reading or sending is exactly one session.
    """
    monkeypatch.chdir(tmp_path)
    path = YTPL.setup_logging(YTPL.DEFAULT_LOG_FILE)

    assert path != YTPL.DEFAULT_LOG_FILE          # the pattern was expanded
    assert '%' not in path
    assert os.path.isfile(path)
    assert os.path.basename(os.path.dirname(path)) == 'logs'


def test_log_file_pattern_is_expanded(clean_logging, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = YTPL.setup_logging('run-%Y.log')

    assert path == time.strftime('run-%Y.log')
    assert os.path.isfile(path)


def test_log_folder_is_created(clean_logging, tmp_path):
    path = YTPL.setup_logging(str(tmp_path / 'deep' / 'nested' / 'run.log'))

    assert path is not None
    assert os.path.isfile(path)


def test_banner_is_intact():
    """
    Ten lines of the art end with a backslash, so it has to be a raw string;
    a plain literal would eat them as escapes.
    """
    lines = YTPL.BANNER.strip(chr(10)).split(chr(10))

    assert len(lines) == 21
    assert sum(1 for l in lines if l.endswith(chr(92))) == 10
    assert chr(13) not in YTPL.BANNER
    assert YTPL.BANNER_WIDTH == max(len(l) for l in lines)


def test_banner_is_printed_on_a_wide_terminal(capsys, monkeypatch):
    monkeypatch.setattr(YTPL.shutil, 'get_terminal_size',
                        lambda *a: os.terminal_size((YTPL.BANNER_WIDTH, 40)))
    YTPL.print_banner()

    assert YTPL.BANNER.strip(chr(10)) in capsys.readouterr().out


def test_narrow_terminal_gets_a_plain_title(capsys, monkeypatch):
    """
    Wrapped, the art is unreadable, so a narrow terminal gets a title line.
    """
    monkeypatch.setattr(YTPL.shutil, 'get_terminal_size',
                        lambda *a: os.terminal_size((80, 40)))
    YTPL.print_banner()

    out = capsys.readouterr().out
    assert 'YTPL - YouTube To PlayList converter' in out
    assert chr(92) not in out


def test_banner_survives_an_unknown_terminal_size(capsys, monkeypatch):
    def _boom(*a):
        raise OSError('no terminal')

    monkeypatch.setattr(YTPL.shutil, 'get_terminal_size', _boom)
    YTPL.print_banner()

    assert 'YTPL' in capsys.readouterr().out
