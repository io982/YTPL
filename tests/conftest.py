"""
Общие фикстуры для тестов YTPL.

Тесты не обращаются к сети: загрузка с YouTube подменяется, а аудио для
проверки анализа пауз генерируется локально через ffmpeg (из imageio-ffmpeg).
"""
import os
import subprocess
import sys
import types

import imageio_ffmpeg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import YTPL  # noqa: E402  (нужен sys.path выше)


# Главы видео-примера из README: 5 «секторов» на 108 минут, то есть внутри
# каждой главы несколько треков — ровно тот случай, ради которого режим по
# умолчанию игнорирует метаданные.
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
    Фабрика тестового аудио. Принимает список элементов:
        ('tone', длительность, частота) — звучащий фрагмент,
        ('silence', длительность)       — тишина.

    Возвращает (путь к wav, общая длительность в секундах).
    Частота дискретизации нарочно низкая (8 кГц, моно) — файлы мелкие,
    а silencedetect работает так же.
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
    «Альбом»: 3 трека по 40 с, между ними паузы по 2 с. Общая длина 124 с —
    короче max_segment_len (300 с), поэтому проверяет, что первый проход по
    паузам выполняется независимо от длины участка.
    Ожидаемые точки реза: середины пауз, то есть ~41 и ~83 с.
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
    Тот же «альбом», но в mp3: split_audio режет без перекодирования (-c copy),
    поэтому на вход ему нужен именно mp3 — как и в реальной работе, где
    download_audio всегда отдаёт mp3.
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
    Непрерывные 700 с без единой паузы: ни один порог тишины не поможет,
    поэтому ожидаем жёсткий рез по max_segment_len.
    """
    return make_audio([('tone', 700, 440)], 'gapless')


@pytest.fixture(scope='session')
def mixed_audio(make_audio):
    """
    Аудио с паузами разной длины: 1.0 с (ниже стартового порога 1.5 с) и 2.0 с.
    На стартовом пороге виден только длинный разрыв; короткий подхватывается
    лишь при понижении порога для слишком длинных участков.
    Раскладка: 100с | 1с | 100с | 2с | 100с  (итого 303 с).
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
    Подменяет download_audio и split_audio: процесс обработки прогоняется без
    сети и без записи файлов. Возвращает функцию-настройку, после вызова
    которой в её атрибуте `.cuts` оказываются точки реза, переданные в нарезку.
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
        # Страховка: в тестах метаданные не должны тянуться из сети.
        monkeypatch.setattr(YTPL, 'get_full_page_text',
                            lambda url: pytest.fail('сетевой запрос в тесте'))
        return state

    setup.state = state
    return setup


@pytest.fixture
def args_factory():
    """
    Собирает объект аргументов так, как его вернул бы argparse.
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
