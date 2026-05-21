"""Unit tests for hk_ipo.l0.orchestrator (the CLI's business-logic layer)."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from hk_ipo.config import Config
from hk_ipo.l0.orchestrator import (
    RunSummary,
    run_backfill,
    run_refresh,
    run_retry_failed,
    run_status,
    run_sync,
    run_verify,
)

PDF_BYTES = b"%PDF-1.4\nhello\n%%EOF\n"
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()

SAMPLE_JSON = {
    "hits": [
        {
            "DOC_ID": "2024010100001",
            "STOCK_CODE": "9999",
            "STOCK_NAME_EN": "Test Holdings",
            "TITLE": "Global Offering",
            "DATE_TIME": "2024-01-15 08:30:00",
            "T1_CODE": "40000", "T2_CODE": "40100",
            "MARKET": "SEHK", "LANGUAGE_CD": "E",
            "FILE_LINK": "https://pdf.test/9999_e.pdf",
        },
        {
            "DOC_ID": "2024010200002",
            "STOCK_CODE": "9998",
            "STOCK_NAME_C": "中文",
            "TITLE": "招股章程",
            "DATE_TIME": "2024-01-22 09:00:00",
            "T1_CODE": "40000", "T2_CODE": "40100",
            "MARKET": "GEM", "LANGUAGE_CD": "C",
            "FILE_LINK": "https://pdf.test/9998_c.pdf",
        },
    ],
    "total": 2, "page": 1, "pageSize": 100,
}


def _cfg(tmp_path: Path) -> Config:
    return Config.from_env(env={
        "HK_IPO_DATA_DIR": str(tmp_path),
        "HKEX_JSON_API_BASE": "https://api.test/titlesearchservlet.do",
        "HKEX_HTML_SEARCH_BASE": "https://api.test/titlesearch.xhtml",
        "HKEX_PDF_BASE": "https://pdf.test",
        "L0_WORKERS": "2",
        "HKEX_CONTACT_EMAIL": "test@example.com",
    })


@respx.mock
@pytest.mark.asyncio
async def test_run_backfill_downloads_english_skips_chinese(tmp_path: Path) -> None:
    respx.get("https://api.test/titlesearchservlet.do").mock(
        return_value=httpx.Response(200, json=SAMPLE_JSON),
    )
    respx.get("https://pdf.test/9999_e.pdf").mock(
        return_value=httpx.Response(200, content=PDF_BYTES),
    )

    cfg = _cfg(tmp_path)
    summary = await run_backfill(
        cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
        workers=2, dry_run=False, limit=None,
    )

    assert isinstance(summary, RunSummary)
    assert summary.discovered == 2
    assert summary.downloaded == 1
    assert summary.skipped_no_english == 1
    assert summary.failed == 0

    pdf_path = cfg.raw_pdfs_dir / "09999.pdf"
    assert pdf_path.exists()
    assert pdf_path.read_bytes() == PDF_BYTES

    manifest = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"]["09999"]["status"] == "success"
    assert manifest["entries"]["09999"]["file_sha256"] == PDF_SHA
    assert manifest["entries"]["09998"]["status"] == "skipped_no_english"


@respx.mock
@pytest.mark.asyncio
async def test_run_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    respx.get("https://api.test/titlesearchservlet.do").mock(
        return_value=httpx.Response(200, json=SAMPLE_JSON),
    )
    cfg = _cfg(tmp_path)
    summary = await run_backfill(
        cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
        workers=2, dry_run=True, limit=None,
    )
    assert summary.discovered == 2
    assert summary.downloaded == 0
    assert not cfg.manifest_path.exists()
    assert list(cfg.raw_pdfs_dir.glob("*.pdf")) == []


@respx.mock
@pytest.mark.asyncio
async def test_run_backfill_idempotent_second_run(tmp_path: Path) -> None:
    respx.get("https://api.test/titlesearchservlet.do").mock(
        return_value=httpx.Response(200, json=SAMPLE_JSON),
    )
    pdf_route = respx.get("https://pdf.test/9999_e.pdf").mock(
        return_value=httpx.Response(200, content=PDF_BYTES),
    )

    cfg = _cfg(tmp_path)
    await run_backfill(cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
                       workers=2, dry_run=False, limit=None)
    first_call = pdf_route.call_count
    summary2 = await run_backfill(cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
                                  workers=2, dry_run=False, limit=None)
    assert pdf_route.call_count == first_call, "second run must not re-download"
    assert summary2.downloaded == 0
    assert summary2.skipped_already_have >= 1


@respx.mock
@pytest.mark.asyncio
async def test_run_refresh_overwrites_when_pdf_changes(tmp_path: Path) -> None:
    respx.get("https://api.test/titlesearchservlet.do").mock(
        return_value=httpx.Response(200, json=SAMPLE_JSON),
    )
    pdf_route = respx.get("https://pdf.test/9999_e.pdf")
    pdf_route.mock(return_value=httpx.Response(200, content=PDF_BYTES))

    cfg = _cfg(tmp_path)
    await run_backfill(cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
                       workers=2, dry_run=False, limit=None)
    orig_hash = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))["entries"]["09999"]["file_sha256"]

    alt_pdf = PDF_BYTES + b"\n%MORE\n"
    pdf_route.mock(return_value=httpx.Response(200, content=alt_pdf))

    summary = await run_refresh(cfg, ["09999"], workers=2)
    assert summary.downloaded == 1
    new_hash = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))["entries"]["09999"]["file_sha256"]
    assert new_hash != orig_hash


@respx.mock
@pytest.mark.asyncio
async def test_run_retry_failed_revisits_failed_entry(tmp_path: Path) -> None:
    respx.get("https://api.test/titlesearchservlet.do").mock(
        return_value=httpx.Response(200, json=SAMPLE_JSON),
    )
    pdf_route = respx.get("https://pdf.test/9999_e.pdf")

    # First attempt: always 503 -> failed.
    pdf_route.mock(return_value=httpx.Response(503))
    cfg = _cfg(tmp_path)
    s1 = await run_backfill(cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
                            workers=2, dry_run=False, limit=None)
    assert s1.failed == 1
    manifest = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"]["09999"]["status"] == "failed"

    # Now allow success.
    pdf_route.mock(return_value=httpx.Response(200, content=PDF_BYTES))
    s2 = await run_retry_failed(cfg, workers=2, max_age_days=None)
    assert s2.downloaded == 1
    manifest2 = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    assert manifest2["entries"]["09999"]["status"] == "success"


def test_run_status_counts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # Empty case:
    assert "empty" in run_status(cfg).lower() or "0" in run_status(cfg)

    # Seed a manifest.
    from hk_ipo.l0.manifest import ManifestStore
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    store = ManifestStore.load(cfg.manifest_path)
    store.upsert(ManifestEntry(
        hk_ticker="09999", status=ManifestStatus.SUCCESS,
        doc_url="x", file_path="09999.pdf", file_sha256="a" * 64,
        file_size_bytes=1, discovered_at=None,
    ))
    store.save()
    out = run_status(cfg)
    assert "success" in out.lower()
    assert "1" in out


def test_run_verify_detects_hash_drift(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.raw_pdfs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.raw_pdfs_dir / "09999.pdf").write_bytes(b"good")

    from hk_ipo.l0.manifest import ManifestStore
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    import hashlib as _h
    good_sha = _h.sha256(b"good").hexdigest()
    store = ManifestStore.load(cfg.manifest_path)
    store.upsert(ManifestEntry(
        hk_ticker="09999", status=ManifestStatus.SUCCESS,
        doc_url="x", file_path="09999.pdf", file_sha256=good_sha,
        file_size_bytes=4, discovered_at=None,
    ))
    store.save()

    code, drift = run_verify(cfg)
    assert code == 0 and drift == []

    # Corrupt the file.
    (cfg.raw_pdfs_dir / "09999.pdf").write_bytes(b"BAD")
    code, drift = run_verify(cfg)
    assert code != 0 and drift == ["09999"]


@respx.mock
def test_run_verify_repair_redownloads_drifted_files(tmp_path: Path) -> None:
    """When repair=True, drifted files must be re-downloaded and verified fixed."""
    cfg = _cfg(tmp_path)
    cfg.raw_pdfs_dir.mkdir(parents=True, exist_ok=True)

    # Write a valid PDF with known hash.
    (cfg.raw_pdfs_dir / "09999.pdf").write_bytes(PDF_BYTES)
    good_sha = hashlib.sha256(PDF_BYTES).hexdigest()

    # Seed manifest with correct hash.
    from hk_ipo.l0.manifest import ManifestStore
    from hk_ipo.l0.models import ManifestEntry, ManifestStatus
    store = ManifestStore.load(cfg.manifest_path)
    store.upsert(ManifestEntry(
        hk_ticker="09999", status=ManifestStatus.SUCCESS,
        doc_url="https://pdf.test/09999_e.pdf", file_path="09999.pdf",
        file_sha256=good_sha, file_size_bytes=len(PDF_BYTES),
        discovered_at=None,
    ))
    store.save()

    # Verify passes initially.
    code, drift = run_verify(cfg)
    assert code == 0 and drift == []

    # Corrupt the file.
    (cfg.raw_pdfs_dir / "09999.pdf").write_bytes(b"BROKEN")

    # Verify detects drift.
    code, drift = run_verify(cfg)
    assert code != 0 and drift == ["09999"]

    # Mock the PDF endpoint for repair re-download.
    respx.get("https://pdf.test/09999_e.pdf").mock(
        return_value=httpx.Response(200, content=PDF_BYTES),
    )

    # Repair must re-download and fix the file.
    code, drift = run_verify(cfg, repair=True)
    assert code == 0, f"repair should fix drift, got code={code}, drift={drift}"
    assert drift == []
    assert (cfg.raw_pdfs_dir / "09999.pdf").read_bytes() == PDF_BYTES
