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


def _html_row(
    ticker: str,
    name: str,
    title: str,
    href: str,
    *,
    headline: str = "Listing Documents - [Offer for Subscription]",
    release_time: str = "15/01/2024 08:30",
    size_kb: int = 4500,
) -> str:
    """Build one HTML <tr> matching the live HKEX titlesearch.xhtml response shape."""
    return f"""
<tr>
  <td class="text-right text-end release-time"><span class="mobile-list-heading">Release Time: </span>{release_time}</td>
  <td class="text-right text-end stock-short-code"><span class="mobile-list-heading">Stock Code: </span>{ticker}</td>
  <td class="stock-short-name"><span class="mobile-list-heading">Stock Short Name: </span>{name}</td>
  <td>
    <div class="headline">{headline}<br/></div>
    <div class="doc-link">
      <a href="{href}" rel="noopener noreferrer" target="_blank">{title}</a>
      (<span class="attachment_filesize">{size_kb}KB</span>)<span class="pdf"></span>
    </div>
  </td>
</tr>"""


def _html_response(rows: list[str]) -> str:
    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html><html><body>
<div class="total-records">Total records found: {len(rows)}</div>
<table><tbody>
{rows_html}
</tbody></table>
</body></html>"""


# Two filings: English (9999) kept, Chinese-only (9998) skipped_no_english.
# Both have headline = "Listing Documents - " so the row passes the headline
# filter; language is inferred from title text by the new parser.
SAMPLE_HTML = _html_response([
    _html_row("09999", "Test Holdings", "Global Offering",
              "https://pdf.test/9999_e.pdf", release_time="15/01/2024 08:30"),
    _html_row("09998", "测试控股", "招股章程",
              "https://pdf.test/9998_c.pdf", release_time="22/01/2024 09:00"),
])


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
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML),
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
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML),
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
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML),
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


# MVP-2 regression: same ticker has both an A1 (wrong_doc_type) and a final
# (kept) filing in the same window. Second backfill must NOT overwrite the
# prior SUCCESS row with SKIPPED_WRONG_DOC_TYPE.
TWO_FILINGS_SAME_TICKER_HTML = _html_response([
    # A1 row: headline starts with "Application Proofs ..." -> is_final=False
    # -> filter classifies as WRONG_DOC_TYPE.
    _html_row(
        "09999", "Test Holdings",
        "APPLICATION PROOF of Test Holdings Limited",
        "https://pdf.test/9999_a1_e.pdf",
        headline="Application Proofs and Post Hearing Information Packs or PHIPs - [PHIP]",
        release_time="10/01/2024 08:00",
    ),
    # Final row: headline starts with "Listing Documents - " -> is_final=True
    # -> filter keeps -> downloaded -> SUCCESS.
    _html_row(
        "09999", "Test Holdings", "Global Offering",
        "https://pdf.test/9999_e.pdf",
        release_time="15/01/2024 08:30",
    ),
])


@respx.mock
@pytest.mark.asyncio
async def test_backfill_second_run_does_not_clobber_success_with_skip(
    tmp_path: Path,
) -> None:
    """MVP-2 / H1 regression.

    First backfill: A1 yielded -> marked WRONG_DOC_TYPE, then final yielded
    -> downloaded -> overwrites with SUCCESS. End state: SUCCESS.

    Second backfill: A1 yielded again. Without the guard, mark_skipped_*
    would overwrite the SUCCESS with SKIPPED_WRONG_DOC_TYPE. With the guard,
    A1 is recognized as 'already have a SUCCESS for this ticker' and skipped.
    """
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=TWO_FILINGS_SAME_TICKER_HTML),
    )
    pdf_route = respx.get("https://pdf.test/9999_e.pdf").mock(
        return_value=httpx.Response(200, content=PDF_BYTES),
    )

    cfg = _cfg(tmp_path)

    # First run: ends with SUCCESS for 09999.
    s1 = await run_backfill(
        cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
        workers=2, dry_run=False, limit=None,
    )
    assert s1.downloaded == 1
    manifest_after_first = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    assert manifest_after_first["entries"]["09999"]["status"] == "success", (
        "first run should land on success"
    )

    # Second run: must remain SUCCESS, no re-download.
    first_call_count = pdf_route.call_count
    s2 = await run_backfill(
        cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
        workers=2, dry_run=False, limit=None,
    )
    assert pdf_route.call_count == first_call_count, "second run must not re-download"
    assert s2.downloaded == 0

    manifest_after_second = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    assert manifest_after_second["entries"]["09999"]["status"] == "success", (
        "second run must NOT overwrite SUCCESS with SKIPPED_WRONG_DOC_TYPE"
    )
    # Both filings (A1 + final) should now be counted as already-have on rerun.
    assert s2.skipped_already_have == 2
    # And we must not have written a fake wrong_doc_type entry anywhere.
    assert s2.skipped_wrong_doc_type == 0


# MVP-3 regression: per-download manifest persistence. With multiple
# successful downloads, manifest.save() must be called incrementally (once
# per success), not just once at the end. This means a mid-batch crash
# preserves the rows that already finished.
THREE_DOWNLOADS_HTML = _html_response([
    _html_row(
        f"0888{i}", f"Test Co {i}", "Global Offering",
        f"https://pdf.test/888{i}_e.pdf",
        release_time="15/01/2024 08:30",
    )
    for i in (1, 2, 3)
])


@respx.mock
@pytest.mark.asyncio
async def test_manifest_is_saved_incrementally_per_download(
    tmp_path: Path, monkeypatch,
) -> None:
    """MVP-3 / H3 regression.

    Spec §5 requires manifest persistence 'after every successful download'.
    The old implementation used asyncio.gather and only wrote AFTER all
    downloads completed — a Ctrl+C mid-batch lost every successful row.

    This test patches ManifestStore.save() to record a snapshot of the
    _entries dict at the moment of each call. With 3 successful downloads,
    we expect save() to be invoked at least 3 times during the download
    loop (one per success), and the SUCCESS rows must be present in the
    snapshot at the moment of save — not all materialised in a single
    final save.
    """
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=THREE_DOWNLOADS_HTML),
    )
    for i in (1, 2, 3):
        respx.get(f"https://pdf.test/888{i}_e.pdf").mock(
            return_value=httpx.Response(200, content=PDF_BYTES),
        )

    from hk_ipo.l0.manifest import ManifestStore
    snapshots: list[dict[str, str]] = []
    original_save = ManifestStore.save

    def _spying_save(self: ManifestStore) -> None:
        snapshot = {
            ticker: entry.status.value
            for ticker, entry in self._entries.items()
        }
        snapshots.append(snapshot)
        original_save(self)

    monkeypatch.setattr(ManifestStore, "save", _spying_save)

    cfg = _cfg(tmp_path)
    summary = await run_backfill(
        cfg, since=date(2024, 1, 1), until=date(2024, 1, 31),
        workers=2, dry_run=False, limit=None,
    )

    assert summary.downloaded == 3, f"expected 3 downloads, got {summary.downloaded}"

    # Count snapshots that contain >= 1 SUCCESS row. This proves that
    # success rows were persisted *during* the download loop, not all at
    # the end. If there was only ONE save call (the gather-then-save bug),
    # we would see exactly one snapshot containing all 3 successes.
    snapshots_with_success = [
        s for s in snapshots if any(v == "success" for v in s.values())
    ]
    assert len(snapshots_with_success) >= 3, (
        f"expected at least 3 save() calls with success rows present "
        f"(one per download), got {len(snapshots_with_success)}. "
        f"All snapshots: {snapshots}"
    )

    # And the count of SUCCESS rows in the snapshot sequence must grow
    # monotonically as downloads complete — not jump 0 -> 3 in one step.
    success_counts = [
        sum(1 for v in s.values() if v == "success")
        for s in snapshots_with_success
    ]
    # The first save with any success should have exactly 1 success (the
    # first download to complete). The last should have 3.
    assert success_counts[0] == 1, (
        f"first save with a success row should have exactly 1 success "
        f"(proving incremental persistence). Got {success_counts}"
    )
    assert max(success_counts) == 3


@respx.mock
@pytest.mark.asyncio
async def test_run_refresh_overwrites_when_pdf_changes(tmp_path: Path) -> None:
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML),
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
    respx.post("https://api.test/titlesearch.xhtml").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML),
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
