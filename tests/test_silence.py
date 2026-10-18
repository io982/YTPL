"""
Анализ пауз: detect_silence_timestamps и вспомогательные функции.

Тесты работают на реальном ffmpeg и сгенерированном локально аудио, поэтому
проверяют весь путь: silencedetect -> отбор кандидатов -> рекурсия по порогам.
"""
import pytest

import YTPL


def test_enforce_min_segment_drops_short_pieces():
    # Резы на 10 и 12 с дают огрызки короче 15 с: остаётся только первый.
    assert YTPL._enforce_min_segment([10.0, 12.0, 40.0], total=100.0, min_seg=15.0) == [40.0]


def test_enforce_min_segment_drops_short_tail():
    # Последний сегмент (100 - 95 = 5 с) слишком короткий — рез отбрасывается.
    assert YTPL._enforce_min_segment([30.0, 95.0], total=100.0, min_seg=15.0) == [30.0]


def test_enforce_min_segment_keeps_valid_cuts():
    assert YTPL._enforce_min_segment([30.0, 60.0], total=100.0, min_seg=15.0) == [30.0, 60.0]


def test_enforce_min_segment_without_cuts():
    assert YTPL._enforce_min_segment([], total=100.0, min_seg=15.0) == []


def test_short_album_is_split_by_gaps(album_audio):
    """
    Главная проверка короткого аудио: 124 с — меньше max_segment_len (300 с),
    но паузы между треками явные, значит резать всё равно нужно.
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration)

    assert len(cuts) == 2
    assert cuts[0] == pytest.approx(41.0, abs=1.0)
    assert cuts[1] == pytest.approx(83.0, abs=1.0)


def test_cuts_land_in_the_middle_of_a_gap(album_audio):
    """
    Рез должен попадать внутрь паузы, а не в звучащий фрагмент: иначе у трека
    отрезается конец или к нему прилипает начало следующего.
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
    При min_segment больше длины трека лишние резы исчезают: ни один рез не
    даёт двух сегментов по 60 с (треки всего по 40 с), поэтому файл остаётся
    целым — вместо набора огрызков.
    """
    path, duration = album_audio

    assert YTPL.detect_silence_timestamps(path, duration, min_segment=60.0) == []
    # Контроль: с порогом по умолчанию резы на этом же файле есть.
    assert len(YTPL.detect_silence_timestamps(path, duration)) == 2


def test_gapless_audio_is_hard_split(gapless_audio):
    """
    700 с без пауз: ни один порог не срабатывает, поэтому участок режется
    жёстко на равные части не длиннее max_segment_len.
    """
    path, duration = gapless_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=300.0)

    bounds = [0.0] + cuts + [duration]
    lengths = [b - a for a, b in zip(bounds, bounds[1:])]

    assert len(cuts) == 2  # ceil(700 / 300) = 3 части
    assert max(lengths) <= 300.0 + 1e-6
    assert max(lengths) - min(lengths) < 1e-6  # части равной длины


def test_threshold_is_lowered_only_for_long_segments(mixed_audio):
    """
    Пауза 1.0 с короче стартового порога (1.5 с), поэтому на первом проходе
    видна только пауза 2.0 с. Остаток длиной ~201 с не превышает
    max_segment_len=300, значит порог не понижается и короткая пауза не режется.
    """
    path, duration = mixed_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=300.0)

    assert len(cuts) == 1
    assert cuts[0] == pytest.approx(201.0, abs=1.5)


def test_long_segment_triggers_lower_threshold(mixed_audio):
    """
    Тот же файл с max_segment_len=150: участок до длинной паузы (~201 с)
    считается слишком длинным, порог понижается до 0.8 с и короткая пауза
    (1.0 с) тоже становится точкой реза.
    """
    path, duration = mixed_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, max_segment_len=150.0)

    assert len(cuts) == 2
    assert cuts[0] == pytest.approx(100.5, abs=1.5)
    assert cuts[1] == pytest.approx(201.0, abs=1.5)


def test_strict_noise_threshold_still_finds_real_gaps(album_audio):
    """
    Паузы между треками — цифровая тишина, поэтому они определяются даже при
    очень строгом пороге громкости (-90 dB).
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, noise_tol='-90dB')

    assert len(cuts) == 2


def test_returns_empty_list_when_nothing_to_cut(gapless_audio):
    """
    Пауз нет, а жёсткий рез не нужен (участок короче max_segment_len):
    функция обязана вернуть пустой список, а не падать.
    """
    path, duration = gapless_audio
    assert YTPL.detect_silence_timestamps(path, duration,
                                          max_segment_len=duration + 10) == []


def test_min_silence_below_floor_still_works(album_audio):
    """
    Вырожденная настройка: стартовый порог ниже floor. Список уровней
    сводится к одному значению, функция не должна падать.
    """
    path, duration = album_audio
    cuts = YTPL.detect_silence_timestamps(path, duration, min_silence_dur=0.5, floor=0.8)

    assert len(cuts) == 2
