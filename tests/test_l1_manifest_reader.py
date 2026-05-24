"""Unit tests for hk_ipo.l1.manifest_reader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------- read_manifest ----------


def test_read_manifest_loads_all_entries() -> None:
    """read_manifest should return a NormalizedEntry for each entry in the manifest."""
    from hk_ipo.l1.manifest_reader import read_manifest

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    assert len(entries) == 6
    tickers = {e.hk_ticker for e in entries}
    assert tickers == {"01810", "00123", "00001", "06666", "09999", "08888"}


def test_read_manifest_success_entry_has_file_path() -> None:
    """Entries with status 'success' should have file_path populated."""
    from hk_ipo.l1.manifest_reader import read_manifest

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    success_entries = [e for e in entries if e.status == "success"]
    assert len(success_entries) == 2
    for e in success_entries:
        assert e.file_path is not None


# ---------- year / month extraction ----------


def test_year_month_from_sehk_url() -> None:
    """Year and month should be parsed from /sehk/YYYY/MMDD/ URLs."""
    from hk_ipo.l1.manifest_reader import read_manifest

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    # 01810 has /sehk/2018/0625/ path
    xiaomi = next(e for e in entries if e.hk_ticker == "01810")
    assert xiaomi.year == 2018
    assert xiaomi.month == 6

    # 00001 has /sehk/2024/0102/ path
    wrong_doc = next(e for e in entries if e.hk_ticker == "00001")
    assert wrong_doc.year == 2024
    assert wrong_doc.month == 1


def test_year_month_from_gem_url() -> None:
    """Year and month should be parsed from /gem/YYYY/MMDD/ URLs."""
    from hk_ipo.l1.manifest_reader import read_manifest

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    # 00123 has /gem/2024/0301/ path
    gem_entry = next(e for e in entries if e.hk_ticker == "00123")
    assert gem_entry.year == 2024
    assert gem_entry.month == 3


def test_year_month_zero_when_doc_url_is_none() -> None:
    """Entries with doc_url=None should get year=0, month=0."""
    from hk_ipo.l1.manifest_reader import read_manifest

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    # 08888 has doc_url: null
    null_url_entry = next(e for e in entries if e.hk_ticker == "08888")
    assert null_url_entry.year == 0
    assert null_url_entry.month == 0


def test_year_month_zero_when_doc_url_does_not_match() -> None:
    """Unrecognized URL formats should yield year=0, month=0."""
    from hk_ipo.l1.manifest_reader import _parse_year_month

    year, month = _parse_year_month("https://example.com/some/other/path/file.pdf")
    assert year == 0
    assert month == 0


def test_parse_year_month_none_input() -> None:
    """None input should yield year=0, month=0."""
    from hk_ipo.l1.manifest_reader import _parse_year_month

    year, month = _parse_year_month(None)
    assert year == 0
    assert month == 0


# ---------- write_normalized ----------


def test_write_normalized_roundtrip(tmp_path: Path) -> None:
    """write_normalized should produce JSON that matches the input entries."""
    from hk_ipo.l1.manifest_reader import read_manifest, write_normalized
    from hk_ipo.l1.models import NormalizedEntry

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    entries = read_manifest(fixture)

    out_path = tmp_path / "normalized.json"
    write_normalized(entries, out_path)

    assert out_path.exists()

    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert len(raw) == 6

    # Spot-check first entry
    first = raw[0]
    assert "hk_ticker" in first
    assert "status" in first
    assert "year" in first
    assert "month" in first
    assert len(first["hk_ticker"]) == 5


def test_write_normalized_creates_parent_dirs(tmp_path: Path) -> None:
    """write_normalized should create missing parent directories."""
    from hk_ipo.l1.manifest_reader import write_normalized
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="01810",
        status="success",
        year=2018,
        month=6,
        company_name_en="Xiaomi Corporation",
        doc_url="https://example.com/file.pdf",
        file_path="01810.pdf",
    )

    deep_path = tmp_path / "a" / "b" / "c" / "output.json"
    write_normalized([entry], deep_path)

    assert deep_path.exists()
    raw = json.loads(deep_path.read_text(encoding="utf-8"))
    assert len(raw) == 1
    assert raw[0]["hk_ticker"] == "01810"


def test_write_normalized_empty_list(tmp_path: Path) -> None:
    """write_normalized should handle an empty list gracefully."""
    from hk_ipo.l1.manifest_reader import write_normalized

    out_path = tmp_path / "empty.json"
    write_normalized([], out_path)

    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert raw == []


def test_write_normalized_then_read_back_matches(tmp_path: Path) -> None:
    """Round-trip: write then re-parse JSON yields same field values."""
    from hk_ipo.l1.manifest_reader import read_manifest, write_normalized

    fixture = Path(__file__).parent / "fixtures" / "sample_manifest.json"
    original = read_manifest(fixture)

    out_path = tmp_path / "roundtrip.json"
    write_normalized(original, out_path)

    # Read back the JSON and compare field-by-field
    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(raw) == len(original)

    for orig, back in zip(original, raw):
        assert back["hk_ticker"] == orig.hk_ticker
        assert back["status"] == orig.status
        assert back["year"] == orig.year
        assert back["month"] == orig.month
        assert back["company_name_en"] == orig.company_name_en
        assert back["doc_url"] == orig.doc_url
        assert back["file_path"] == orig.file_path
