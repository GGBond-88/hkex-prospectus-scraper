"""Unit tests for hk_ipo.l0.models."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------- pad_ticker ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9999", "09999"),
        ("00001", "00001"),
        ("1", "00001"),
        ("12345", "12345"),
        (9999, "09999"),
        (1, "00001"),
    ],
)
def test_pad_ticker_normalizes_to_5_digits(raw: object, expected: str) -> None:
    from hk_ipo.l0.models import pad_ticker
    assert pad_ticker(raw) == expected


@pytest.mark.parametrize("bad", ["", "abc", "123456", "12.3", None, -1])
def test_pad_ticker_rejects_invalid_input(bad: object) -> None:
    from hk_ipo.l0.models import pad_ticker
    with pytest.raises(ValueError):
        pad_ticker(bad)


# ---------- Filing ----------


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_filing_minimal_construction() -> None:
    from hk_ipo.l0.models import Filing
    f = Filing(
        hk_ticker="09999",
        doc_id="2024010100001",
        doc_title="Global Offering",
        doc_url="https://www1.hkexnews.hk/listedco/listconews/sehk/2024/0115/2024010100001_e.pdf",
        doc_type="Prospectus",
        market="MB",
        language="en",
        is_final=True,
        publish_date=_utc(2024, 1, 15),
        company_name_en="Test Holdings Limited",
    )
    assert f.hk_ticker == "09999"
    assert f.is_final is True
    assert f.language == "en"


def test_filing_pads_4_digit_ticker_in_constructor() -> None:
    from hk_ipo.l0.models import Filing
    f = Filing(
        hk_ticker="9999",  # 4 digits, must be padded
        doc_id="d",
        doc_title="t",
        doc_url="u",
        doc_type="Prospectus",
        market="MB",
        language="en",
        is_final=True,
        publish_date=_utc(2024, 1, 15),
    )
    assert f.hk_ticker == "09999"


def test_filing_market_must_be_mb_or_gem() -> None:
    from hk_ipo.l0.models import Filing
    with pytest.raises(ValueError):
        Filing(
            hk_ticker="09999", doc_id="d", doc_title="t", doc_url="u",
            doc_type="Prospectus", market="NYSE",  # invalid
            language="en", is_final=True, publish_date=_utc(2024, 1, 15),
        )


def test_filing_language_must_be_known() -> None:
    from hk_ipo.l0.models import Filing
    with pytest.raises(ValueError):
        Filing(
            hk_ticker="09999", doc_id="d", doc_title="t", doc_url="u",
            doc_type="Prospectus", market="MB",
            language="fr",  # invalid
            is_final=True, publish_date=_utc(2024, 1, 15),
        )


def test_filing_company_name_handles_non_ascii() -> None:
    from hk_ipo.l0.models import Filing
    f = Filing(
        hk_ticker="09999", doc_id="d", doc_title="招股", doc_url="u",
        doc_type="Prospectus", market="MB", language="en", is_final=True,
        publish_date=_utc(2024, 1, 15),
        company_name_zh="测试控股有限公司",
    )
    assert f.company_name_zh == "测试控股有限公司"


# ---------- ManifestStatus ----------


def test_manifest_status_closed_vocabulary() -> None:
    from hk_ipo.l0.models import ManifestStatus
    expected = {"success", "skipped_no_english", "skipped_wrong_doc_type", "failed", "pending"}
    assert {s.value for s in ManifestStatus} == expected


# ---------- ManifestEntry ----------


def test_manifest_entry_success_round_trip_dict() -> None:
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    entry = ManifestEntry(
        hk_ticker="09999",
        status=ManifestStatus.SUCCESS,
        doc_id="2024010100001",
        doc_url="https://x/y.pdf",
        file_path="09999.pdf",
        file_sha256="a" * 64,
        file_size_bytes=12345,
        downloaded_at=_utc(2024, 1, 15),
        discovered_at=_utc(2024, 1, 15),
        company_name_en="Test Holdings",
        listing_date="2024-07-15",
        market="MB",
        doc_title="Global Offering",
        language="en",
    )
    d = entry.to_dict()
    assert d["status"] == "success"
    assert d["hk_ticker"] == "09999"
    assert d["downloaded_at"].endswith("Z") or "+00:00" in d["downloaded_at"]

    back = ManifestEntry.from_dict(d)
    assert back == entry


def test_manifest_entry_skipped_no_english() -> None:
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    entry = ManifestEntry(
        hk_ticker="09998",
        status=ManifestStatus.SKIPPED_NO_ENGLISH,
        discovered_at=_utc(2024, 1, 22),
        doc_id="2024010200002",
        doc_url="https://x/y_c.pdf",
        skip_reason="no_english_version_published",
        company_name_zh="测试控股",
    )
    d = entry.to_dict()
    assert d["status"] == "skipped_no_english"
    assert "file_sha256" not in d or d.get("file_sha256") is None
    back = ManifestEntry.from_dict(d)
    assert back == entry


def test_manifest_entry_failed_tracks_attempts() -> None:
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    entry = ManifestEntry(
        hk_ticker="09997",
        status=ManifestStatus.FAILED,
        doc_url="https://x/z.pdf",
        error="HTTPError: 503 after 5 retries",
        first_attempted_at=_utc(2024, 1, 1),
        last_attempted_at=_utc(2024, 1, 1),
        attempt_count=5,
        discovered_at=_utc(2024, 1, 1),
    )
    assert entry.attempt_count == 5
    back = ManifestEntry.from_dict(entry.to_dict())
    assert back.attempt_count == 5


def test_manifest_entry_rejects_unknown_status_from_dict() -> None:
    from hk_ipo.l0.models import ManifestEntry
    with pytest.raises(ValueError):
        ManifestEntry.from_dict({"hk_ticker": "09999", "status": "bogus_state"})


# ---------- DownloadResult ----------


def test_download_result_success() -> None:
    from hk_ipo.l0.models import DownloadOutcome, DownloadResult
    r = DownloadResult(
        hk_ticker="09999",
        outcome=DownloadOutcome.SUCCESS,
        file_path="09999.pdf",
        file_sha256="a" * 64,
        file_size_bytes=42,
        attempts=1,
    )
    assert r.is_success is True


def test_download_result_failure_records_error() -> None:
    from hk_ipo.l0.models import DownloadOutcome, DownloadResult
    r = DownloadResult(
        hk_ticker="09999",
        outcome=DownloadOutcome.FAILED,
        attempts=5,
        error="HTTPError: 503 after 5 retries",
    )
    assert r.is_success is False
    assert r.error is not None
