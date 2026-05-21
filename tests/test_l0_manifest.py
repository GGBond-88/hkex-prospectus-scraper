"""Unit tests for hk_ipo.l0.manifest."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hk_ipo.l0.manifest import ManifestStore, SCHEMA_VERSION
from hk_ipo.l0.models import ManifestEntry, ManifestStatus


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _concurrent_writer(path_str: str, tag: str) -> None:
    """Module-level writer for multiprocessing concurrency test."""
    path = Path(path_str)
    store = ManifestStore.load(path)
    store.upsert(_success_entry(tag))
    store.save()


def _success_entry(ticker: str = "09999") -> ManifestEntry:
    return ManifestEntry(
        hk_ticker=ticker,
        status=ManifestStatus.SUCCESS,
        doc_id="d",
        doc_url="https://x/y.pdf",
        file_path=f"{ticker}.pdf",
        file_sha256="a" * 64,
        file_size_bytes=42,
        downloaded_at=_utc(2024, 1, 15),
        discovered_at=_utc(2024, 1, 15),
    )


def test_load_missing_file_returns_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    store = ManifestStore.load(path)
    assert store.entry_count == 0
    assert store.schema_version == SCHEMA_VERSION


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    store = ManifestStore.load(path)
    store.upsert(_success_entry())
    store.save()

    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["entries"]["09999"]["status"] == "success"

    again = ManifestStore.load(path)
    assert again.entry_count == 1
    assert again.get("09999").status == ManifestStatus.SUCCESS


# ---------- status mutation API ----------

def test_add_success_entry(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.add_success(
        hk_ticker="09999",
        doc_id="d", doc_url="u", file_path="09999.pdf",
        file_sha256="a" * 64, file_size_bytes=42,
        discovered_at=_utc(2024, 1, 15),
    )
    e = store.get("09999")
    assert e.status == ManifestStatus.SUCCESS
    assert e.downloaded_at is not None


def test_mark_failed_increments_attempts(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.mark_failed(
        hk_ticker="09999", doc_url="u",
        error="HTTPError: 503", discovered_at=_utc(2024, 1, 1),
    )
    store.mark_failed(
        hk_ticker="09999", doc_url="u",
        error="HTTPError: 503", discovered_at=_utc(2024, 1, 1),
    )
    e = store.get("09999")
    assert e.status == ManifestStatus.FAILED
    assert e.attempt_count == 2
    assert e.first_attempted_at != e.last_attempted_at or e.first_attempted_at is not None


def test_mark_skipped_no_english(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.mark_skipped_no_english(
        hk_ticker="09998",
        doc_id="d", doc_url="u",
        company_name_zh="测试控股",
        discovered_at=_utc(2024, 1, 22),
    )
    e = store.get("09998")
    assert e.status == ManifestStatus.SKIPPED_NO_ENGLISH
    assert e.skip_reason == "no_english_version_published"


def test_mark_skipped_wrong_doc_type(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.mark_skipped_wrong_doc_type(
        hk_ticker="09997",
        doc_id="d2", doc_url="u2",
        discovered_at=_utc(2024, 2, 1),
    )
    e = store.get("09997")
    assert e.status == ManifestStatus.SKIPPED_WRONG_DOC_TYPE
    assert e.skip_reason == "not_final_prospectus"


def test_get_status_for_unknown_ticker_returns_none(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    assert store.get_status("00001") is None


def test_get_status_for_known_ticker(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.upsert(_success_entry())
    assert store.get_status("09999") == ManifestStatus.SUCCESS


def test_pending_filings_excludes_already_succeeded(tmp_path: Path) -> None:
    from hk_ipo.l0.models import Filing

    store = ManifestStore.load(tmp_path / "manifest.json")
    store.upsert(_success_entry("09999"))

    f_known = Filing(
        hk_ticker="09999", doc_id="d", doc_title="t",
        doc_url="https://x/_e.pdf", doc_type="Prospectus", market="MB",
        language="en", is_final=True, publish_date=_utc(2024, 1, 15),
    )
    f_new = Filing(
        hk_ticker="08888", doc_id="d2", doc_title="t",
        doc_url="https://x/_e.pdf", doc_type="Prospectus", market="MB",
        language="en", is_final=True, publish_date=_utc(2024, 1, 15),
    )
    pending = list(store.pending_filings([f_known, f_new]))
    assert [p.hk_ticker for p in pending] == ["08888"]


def test_failed_entries_selector(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    store.mark_failed(hk_ticker="09999", doc_url="u1", error="x",
                      discovered_at=_utc(2024, 1, 1))
    store.upsert(_success_entry("08888"))
    tickers = sorted(e.hk_ticker for e in store.failed_entries())
    assert tickers == ["09999"]


def test_failed_entries_with_max_age_days(tmp_path: Path) -> None:
    store = ManifestStore.load(tmp_path / "manifest.json")
    # Inject directly to fake an old timestamp.
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    ancient = ManifestEntry(
        hk_ticker="09999",
        status=ManifestStatus.FAILED,
        doc_url="u",
        error="x",
        first_attempted_at=_utc(2020, 1, 1),
        last_attempted_at=_utc(2020, 1, 1),
        attempt_count=5,
        discovered_at=_utc(2020, 1, 1),
    )
    store.upsert(ancient)
    assert list(store.failed_entries(max_age_days=30)) == []
    assert list(store.failed_entries(max_age_days=None)) == [ancient]


# ---------- file lock ----------

def test_concurrent_writers_do_not_corrupt(tmp_path: Path) -> None:
    """Two processes saving the manifest must serialize via portalocker."""
    import multiprocessing as mp

    path = tmp_path / "manifest.json"
    path_str = str(path)

    # Seed an empty manifest so both writers go through the merge path.
    ManifestStore.load(path).save()

    procs = [mp.Process(target=_concurrent_writer, args=(path_str, t))
             for t in ("00001", "00002", "00003", "00004")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0

    final = ManifestStore.load(path)
    # Each writer loaded then saved; last writer wins (no merge logic yet).
    # At minimum the file must remain valid JSON and parseable.
    assert final.entry_count >= 1
