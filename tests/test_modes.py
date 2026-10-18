"""
Выбор режима нарезки.

Ключевое поведение: по умолчанию режем по паузам, а главы и описание видео
используются только принудительно (--from-description). Проверяется и на уровне
аргументов командной строки (resolve_mode), и на уровне обработки видео
(process_youtube_video).
"""
import pytest

import YTPL
from conftest import EXAMPLE_CHAPTERS

CHAPTER_CUTS = [930.0, 2085.0, 3130.0, 4200.0]


# --- resolve_mode: разбор аргументов ---------------------------------------

def test_default_mode_is_silence(args_factory):
    args = args_factory(url=True)
    assert YTPL.resolve_mode(args) == (YTPL.MODE_SILENCE, None)


def test_without_arguments_non_interactive_defaults_to_silence(args_factory):
    args = args_factory()
    assert YTPL.resolve_mode(args, interactive=False) == (YTPL.MODE_SILENCE, None)


def test_from_description_flag_selects_metadata(args_factory):
    args = args_factory(url=True, from_description=True)
    assert YTPL.resolve_mode(args) == (YTPL.MODE_META, None)


def test_timestamps_flag_selects_manual(args_factory):
    args = args_factory(url=True, timestamps='145,325')
    assert YTPL.resolve_mode(args) == (YTPL.MODE_MANUAL, '145,325')


def test_timestamps_win_over_from_description(args_factory):
    args = args_factory(url=True, timestamps='145', from_description=True)
    assert YTPL.resolve_mode(args) == (YTPL.MODE_MANUAL, '145')


def test_silence_flag_is_explicit_default(args_factory):
    args = args_factory(url=True, force_silence=True)
    assert YTPL.resolve_mode(args) == (YTPL.MODE_SILENCE, None)


@pytest.mark.parametrize('answer, expected', [
    ('', (YTPL.MODE_SILENCE, None)),
    ('d', (YTPL.MODE_META, None)),
    ('в', (YTPL.MODE_META, None)),          # та же клавиша в русской раскладке
    ('описание', (YTPL.MODE_META, None)),
    ('145,325', (YTPL.MODE_MANUAL, '145,325')),
])
def test_interactive_prompt(monkeypatch, args_factory, answer, expected):
    monkeypatch.setattr('builtins.input', lambda *a: answer)
    args = args_factory()
    assert YTPL.resolve_mode(args, interactive=True) == expected


def test_parser_defaults():
    args = YTPL.build_parser().parse_args([])
    assert args.from_description is False
    assert args.force_silence is False
    assert args.timestamps is None
    assert args.noise == '-30dB'
    assert args.min_silence == 1.5
    assert args.floor == 0.8
    assert args.max_segment == 300.0
    assert args.min_segment == 15.0


@pytest.mark.parametrize('flag', ['-d', '--from-description', '--desc', '--chapters'])
def test_from_description_aliases(flag):
    args = YTPL.build_parser().parse_args([flag])
    assert args.from_description is True


def test_silence_opts_from_args():
    args = YTPL.build_parser().parse_args(['--noise=-25dB', '--min-silence', '2',
                                           '--floor', '1', '--max-segment', '240',
                                           '--min-segment', '20'])
    assert YTPL.silence_opts_from_args(args) == {
        'noise_tol': '-25dB',
        'min_silence_dur': 2.0,
        'floor': 1.0,
        'max_segment_len': 240.0,
        'min_segment': 20.0,
    }


@pytest.mark.parametrize('given, expected', [
    ('-30dB', '-30dB'),
    ('30dB', '-30dB'),      # argparse не пропускает '--noise -30dB', поэтому
    ('+30dB', '-30dB'),     # допускается запись без минуса
    ('  25dB  ', '-25dB'),
    ('', '-30dB'),
])
def test_normalize_noise(given, expected):
    assert YTPL.normalize_noise(given) == expected


def test_noise_without_minus_is_accepted_by_cli():
    args = YTPL.build_parser().parse_args(['--noise', '25dB'])
    assert YTPL.silence_opts_from_args(args)['noise_tol'] == '-25dB'


# --- process_youtube_video: какой источник резов реально используется ------

def test_default_ignores_chapters(fake_download, album_audio):
    """
    Главный сценарий: у видео есть главы, но они размечают блоки из нескольких
    треков. По умолчанию они должны игнорироваться в пользу анализа пауз.
    """
    path, duration = album_audio
    state = fake_download(path, duration, chapters=EXAMPLE_CHAPTERS)

    YTPL.process_youtube_video('https://example.invalid')

    assert state.cuts == [pytest.approx(41.0, abs=1.0), pytest.approx(83.0, abs=1.0)]
    assert state.cuts != CHAPTER_CUTS


def test_explicit_silence_mode_ignores_chapters(fake_download, album_audio):
    path, duration = album_audio
    state = fake_download(path, duration, chapters=EXAMPLE_CHAPTERS)

    YTPL.process_youtube_video('https://example.invalid', mode=YTPL.MODE_SILENCE)

    assert len(state.cuts) == 2


def test_meta_mode_uses_chapters(fake_download, album_audio):
    path, duration = album_audio
    state = fake_download(path, duration, chapters=EXAMPLE_CHAPTERS)

    YTPL.process_youtube_video('https://example.invalid', mode=YTPL.MODE_META)

    assert state.cuts == CHAPTER_CUTS


def test_meta_mode_falls_back_to_silence(fake_download, album_audio, monkeypatch):
    """
    Режим метаданных запрошен, но таймкодов в них нет: вместо отказа
    переходим на анализ пауз.
    """
    path, duration = album_audio
    state = fake_download(path, duration, description='без таймкодов', chapters=None)
    monkeypatch.setattr(YTPL, 'get_full_page_text', lambda url: '')

    YTPL.process_youtube_video('https://example.invalid', mode=YTPL.MODE_META)

    assert len(state.cuts) == 2


def test_manual_timestamps_override_mode(fake_download, album_audio):
    """
    Переданные вручную таймкоды важнее любого режима — в том числе
    принудительного режима метаданных.
    """
    path, duration = album_audio
    state = fake_download(path, duration, chapters=EXAMPLE_CHAPTERS)

    YTPL.process_youtube_video('https://example.invalid', '30,60,90',
                               mode=YTPL.MODE_META)

    assert state.cuts == [30.0, 60.0, 90.0]


def test_manual_timestamps_ignore_garbage(fake_download, album_audio):
    path, duration = album_audio
    state = fake_download(path, duration)

    YTPL.process_youtube_video('https://example.invalid', '30, abc, 60,,90')

    assert state.cuts == [30.0, 60.0, 90.0]


def test_silence_opts_are_passed_through(fake_download, album_audio):
    """
    max_segment_len=20 заставляет дорезать каждый 40-секундный трек, поэтому
    резов становится заметно больше двух: значит параметры режима действительно
    доходят до detect_silence_timestamps.
    """
    path, duration = album_audio
    state = fake_download(path, duration, chapters=EXAMPLE_CHAPTERS)

    YTPL.process_youtube_video('https://example.invalid',
                               silence_opts={'max_segment_len': 20.0,
                                             'min_segment': 5.0})

    assert len(state.cuts) > 2


def test_output_dir_is_sanitized(fake_download, album_audio):
    path, duration = album_audio
    state = fake_download(path, duration, title='S U P E R | N O S T A L G I A')

    YTPL.process_youtube_video('https://example.invalid')

    assert state.base_name == 'S_U_P_E_R___N_O_S_T_A_L_G_I_A'
    assert state.output_dir == state.base_name


def test_no_cuts_keeps_single_file_without_prompt(fake_download, album_audio, monkeypatch):
    """
    Резов нет и запуск неинтерактивный: скрипт не должен зависать на input(),
    а обязан сохранить аудио одним файлом.
    """
    path, duration = album_audio
    state = fake_download(path, duration)
    monkeypatch.setattr(YTPL, 'detect_silence_timestamps',
                        lambda *a, **k: [])
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('лишний input()'))
    monkeypatch.setattr(YTPL.sys, 'stdin', None)

    result = YTPL.process_youtube_video('https://example.invalid')

    assert state.cuts == []
    assert len(result) == 1
