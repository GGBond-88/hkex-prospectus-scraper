"""Unit tests for hk_ipo.l1.models."""
from __future__ import annotations

from datetime import date

import pytest


# ---------- NormalizedEntry ----------


def test_normalized_entry_success_construction() -> None:
    """A success entry should store all fields and pad the ticker."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="1810",
        status="success",
        year=2018,
        month=6,
        company_name_en="Xiaomi Corporation",
        doc_url="https://www1.hkexnews.hk/listedco/listconews/sehk/2018/0625/file_e.pdf",
        file_path="01810.pdf",
    )
    assert entry.hk_ticker == "01810"
    assert entry.status == "success"
    assert entry.year == 2018
    assert entry.month == 6
    assert entry.company_name_en == "Xiaomi Corporation"
    assert entry.file_path == "01810.pdf"


def test_normalized_entry_ticker_padded_from_int() -> None:
    """Ticker should be zero-padded to 5 digits even when passed as int."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker=123,  # type: ignore[arg-type]
        status="success",
        year=2024,
        month=1,
        company_name_en=None,
        doc_url=None,
        file_path=None,
    )
    assert entry.hk_ticker == "00123"


def test_normalized_entry_ticker_padded_from_short_str() -> None:
    """Ticker should be zero-padded to 5 digits."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="1",
        status="success",
        year=2024,
        month=1,
        company_name_en=None,
        doc_url=None,
        file_path=None,
    )
    assert entry.hk_ticker == "00001"


def test_normalized_entry_is_frozen() -> None:
    """NormalizedEntry should be immutable."""
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
    with pytest.raises(Exception):
        entry.year = 2020  # type: ignore[misc]


def test_normalized_entry_skipped_no_english() -> None:
    """Entry with skipped_no_english status should have file_path as None."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="06666",
        status="skipped_no_english",
        year=2024,
        month=3,
        company_name_en=None,
        doc_url="https://www1.hkexnews.hk/listedco/listconews/sehk/2024/0315/file_c.pdf",
        file_path=None,
    )
    assert entry.status == "skipped_no_english"
    assert entry.file_path is None


def test_normalized_entry_failed_without_file_path() -> None:
    """Failed entry should have file_path as None."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="09999",
        status="failed",
        year=2024,
        month=6,
        company_name_en=None,
        doc_url="https://www1.hkexnews.hk/listedco/listconews/gem/2024/0615/file_e.pdf",
        file_path=None,
    )
    assert entry.status == "failed"
    assert entry.file_path is None


def test_normalized_entry_year_month_zero_for_no_url() -> None:
    """When no doc_url is available, year and month should be 0."""
    from hk_ipo.l1.models import NormalizedEntry

    entry = NormalizedEntry(
        hk_ticker="08888",
        status="skipped_wrong_doc_type",
        year=0,
        month=0,
        company_name_en=None,
        doc_url=None,
        file_path=None,
    )
    assert entry.year == 0
    assert entry.month == 0


# ---------- ExternalIPO ----------


def test_external_ipo_construction() -> None:
    """ExternalIPO should store source provenance and pad ticker."""
    from hk_ipo.l1.models import ExternalIPO

    ipo = ExternalIPO(
        hk_ticker="1810",
        company_name="Xiaomi Corporation",
        list_date=date(2018, 7, 9),
        source="hkex_stats",
        source_url="https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/All-Securities-List",
    )
    assert ipo.hk_ticker == "01810"
    assert ipo.company_name == "Xiaomi Corporation"
    assert ipo.list_date == date(2018, 7, 9)
    assert ipo.source == "hkex_stats"


def test_external_ipo_list_date_none_allowed() -> None:
    """list_date may be None for year-only sources."""
    from hk_ipo.l1.models import ExternalIPO

    ipo = ExternalIPO(
        hk_ticker="00001",
        company_name="CK Hutchison",
        list_date=None,
        source="wikipedia",
        source_url="https://en.wikipedia.org/wiki/List_of_companies_listed_on_HKSE",
    )
    assert ipo.list_date is None


def test_external_ipo_ticker_padded() -> None:
    """Ticker is padded via pad_ticker in __post_init__."""
    from hk_ipo.l1.models import ExternalIPO

    ipo = ExternalIPO(
        hk_ticker=1,  # type: ignore[arg-type]
        company_name="Test Co",
        list_date=None,
        source="aastocks",
        source_url="https://example.com",
    )
    assert ipo.hk_ticker == "00001"


def test_external_ipo_is_frozen() -> None:
    """ExternalIPO should be immutable."""
    from hk_ipo.l1.models import ExternalIPO

    ipo = ExternalIPO(
        hk_ticker="01810",
        company_name="Xiaomi Corporation",
        list_date=date(2018, 7, 9),
        source="hkex_stats",
        source_url="https://example.com",
    )
    with pytest.raises(Exception):
        ipo.source = "other"  # type: ignore[misc]


# ---------- GapReport ----------


def test_gap_report_construction() -> None:
    """GapReport should hold all gap-analysis fields."""
    from hk_ipo.l1.models import GapReport

    report = GapReport(
        period=(date(2024, 1, 1), date(2024, 6, 30)),
        manifest_success={"01810", "00123"},
        manifest_skipped={"00001": "wrong_doc_type", "06666": "no_english"},
        by_source={
            "hkex_stats": {"01810", "00123", "09999"},
            "aastocks": {"01810", "00123", "08888"},
        },
        missing_from_manifest={"09999"},
        wrongly_skipped=set(),
        extra_in_manifest={"00123"},
        single_source_candidates={"08888"},
        per_year_counts={2024: {"hkex_stats": 50, "aastocks": 48}},
    )
    assert report.period == (date(2024, 1, 1), date(2024, 6, 30))
    assert report.manifest_success == {"01810", "00123"}
    assert report.manifest_skipped["00001"] == "wrong_doc_type"
    assert report.by_source["hkex_stats"] == {"01810", "00123", "09999"}
    assert report.missing_from_manifest == {"09999"}
    assert report.wrongly_skipped == set()
    assert report.extra_in_manifest == {"00123"}
    assert report.single_source_candidates == {"08888"}
    assert report.per_year_counts[2024]["hkex_stats"] == 50


def test_gap_report_ticker_sets_are_padded() -> None:
    """All ticker strings in GapReport sets should be 5-digit padded.
    The caller is responsible for padding; we test that the structure works."""
    from hk_ipo.l1.models import GapReport

    report = GapReport(
        period=(date(2024, 1, 1), date(2024, 12, 31)),
        manifest_success={"01810", "09999"},
        manifest_skipped={},
        by_source={"hkex_stats": {"01810"}},
        missing_from_manifest=set(),
        wrongly_skipped=set(),
        extra_in_manifest=set(),
        single_source_candidates=set(),
        per_year_counts={},
    )
    # All tickers should be 5-char strings
    for t in report.manifest_success:
        assert len(t) == 5


def test_gap_report_is_frozen() -> None:
    """GapReport should be immutable."""
    from hk_ipo.l1.models import GapReport

    report = GapReport(
        period=(date(2024, 1, 1), date(2024, 12, 31)),
        manifest_success=set(),
        manifest_skipped={},
        by_source={},
        missing_from_manifest=set(),
        wrongly_skipped=set(),
        extra_in_manifest=set(),
        single_source_candidates=set(),
        per_year_counts={},
    )
    with pytest.raises(Exception):
        report.missing_from_manifest = {"00001"}  # type: ignore[misc]
