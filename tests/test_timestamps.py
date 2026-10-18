"""
Timestamp parsing: parse_time, extract_timestamps, timestamps_from_metadata.
"""
import pytest

import YTPL
from conftest import EXAMPLE_CHAPTERS, EXAMPLE_DESCRIPTION


@pytest.mark.parametrize('text, expected', [
    ('145s', 145),
    ('0s', 0),
    ('2m25s', 145),
    ('3m', 180),
    ('1:30', 90),
    ('1:30:25', 5425),
    ('0:00', 0),
    ('  90  ', 90),
])
def test_parse_time(text, expected):
    assert YTPL.parse_time(text) == expected


def test_extract_timestamps_url_params():
    text = 'https://youtu.be/abc?t=145s and https://youtu.be/abc&t=2m25s and ?t=300'
    assert YTPL.extract_timestamps(text) == [145, 300]


def test_extract_timestamps_text_codes():
    text = 'Tracklist:\n0:00 intro\n2:25 second\n1:30:25 finale'
    # The zero timestamp is dropped: it is the start of the file, nothing to cut.
    assert YTPL.extract_timestamps(text) == [145, 5425]


def test_extract_timestamps_sorted_and_deduplicated():
    text = '5:00 here\n1:00 there\n5:00 again\n?t=60'
    assert YTPL.extract_timestamps(text) == [60, 300]


def test_extract_timestamps_empty_text():
    assert YTPL.extract_timestamps('') == []
    assert YTPL.extract_timestamps('no timestamps at all') == []


def test_metadata_prefers_chapters(monkeypatch):
    monkeypatch.setattr(YTPL, 'get_full_page_text',
                        lambda url: pytest.fail('the page is not needed, chapters exist'))
    timestamps, source = YTPL.timestamps_from_metadata(
        'https://example.invalid', EXAMPLE_DESCRIPTION, EXAMPLE_CHAPTERS)
    assert timestamps == [930.0, 2085.0, 3130.0, 4200.0]
    assert 'chapters' in source


def test_metadata_falls_back_to_description(monkeypatch):
    monkeypatch.setattr(YTPL, 'get_full_page_text',
                        lambda url: pytest.fail('the page is not needed, description has codes'))
    timestamps, source = YTPL.timestamps_from_metadata(
        'https://example.invalid', EXAMPLE_DESCRIPTION, chapters=None)
    assert timestamps == [930, 2085, 3130, 4200]
    assert source == 'video description'


def test_metadata_falls_back_to_page(monkeypatch):
    monkeypatch.setattr(YTPL, 'get_full_page_text', lambda url: '0:00 intro\n3:20 second')
    timestamps, source = YTPL.timestamps_from_metadata(
        'https://example.invalid', description='', chapters=None)
    assert timestamps == [200]
    assert source == 'video page HTML'


def test_metadata_empty_when_nothing_found(monkeypatch):
    monkeypatch.setattr(YTPL, 'get_full_page_text', lambda url: 'nothing useful')
    timestamps, source = YTPL.timestamps_from_metadata(
        'https://example.invalid', description='', chapters=None)
    assert timestamps == []
    assert source == 'nothing found'


def test_metadata_ignores_chapter_at_zero():
    # The start of the first chapter (0 s) is not a cut point.
    chapters = [{'start_time': 0}, {'start_time': 61.5}]
    timestamps, _ = YTPL.timestamps_from_metadata('https://example.invalid', '', chapters)
    assert timestamps == [61.5]
