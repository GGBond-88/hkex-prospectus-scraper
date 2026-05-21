"""Unit tests for hk_ipo.l0.downloader."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from hk_ipo.l0.downloader import PDFDownloader, sweep_orphan_tmp_files
from hk_ipo.l0.models import DownloadOutcome, Filing


PDF_BYTES = b"%PDF-1.4\nfake-content\n%%EOF\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()


def _filing(ticker: str = "09999", url: str = "https://example.test/p.pdf") -> Filing:
    return Filing(
        hk_ticker=ticker,
        doc_id="d",
        doc_title="Global Offering",
        doc_url=url,
        doc_type="Prospectus",
        market="MB",
        language="en",
        is_final=True,
        publish_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
    )


# ---------- sweep_orphan_tmp_files ----------

def test_sweep_orphan_removes_only_pdf_tmp(tmp_path: Path) -> None:
    (tmp_path / "09999.pdf").write_bytes(b"keep")
    (tmp_path / "09999.pdf.tmp").write_bytes(b"orphan")
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")

    removed = sweep_orphan_tmp_files(tmp_path)

    assert removed == 1
    assert not (tmp_path / "09999.pdf.tmp").exists()
    assert (tmp_path / "09999.pdf").exists()
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "notes.txt").exists()


def test_sweep_orphan_on_empty_dir_is_zero(tmp_path: Path) -> None:
    assert sweep_orphan_tmp_files(tmp_path) == 0


def test_sweep_orphan_on_missing_dir_is_zero(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    assert sweep_orphan_tmp_files(missing) == 0


# ---------- PDFDownloader.download single ----------

@respx.mock
@pytest.mark.asyncio
async def test_download_success_writes_pdf_and_returns_hash(tmp_path: Path) -> None:
    url = "https://example.test/p.pdf"
    respx.get(url).mock(return_value=httpx.Response(200, content=PDF_BYTES))

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, user_agent="hk-ipo-research/0.1 (test)", jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.SUCCESS
    assert result.file_path == "09999.pdf"
    assert result.file_sha256 == PDF_SHA256
    assert result.file_size_bytes == len(PDF_BYTES)
    assert (tmp_path / "09999.pdf").read_bytes() == PDF_BYTES
    assert not (tmp_path / "09999.pdf.tmp").exists()


@respx.mock
@pytest.mark.asyncio
async def test_download_4xx_is_terminal_failure(tmp_path: Path) -> None:
    url = "https://example.test/p.pdf"
    respx.get(url).mock(return_value=httpx.Response(404))

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.FAILED
    assert "404" in (result.error or "")
    assert not (tmp_path / "09999.pdf").exists()
    assert not (tmp_path / "09999.pdf.tmp").exists()


@respx.mock
@pytest.mark.asyncio
async def test_download_retries_on_503_then_succeeds(tmp_path: Path) -> None:
    url = "https://example.test/p.pdf"
    route = respx.get(url).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, content=PDF_BYTES),
        ],
    )

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, per_request_max_attempts=5, jitter_seconds=(0.0, 0.0))
    # Speed up the wait between attempts in tests:
    dl._sem = type(dl._sem)(1)  # noqa: SLF001
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.SUCCESS, result.error
    assert route.call_count == 3
    assert (tmp_path / "09999.pdf").exists()


@respx.mock
@pytest.mark.asyncio
async def test_download_exhausts_retries_then_fails(tmp_path: Path) -> None:
    url = "https://example.test/p.pdf"
    respx.get(url).mock(return_value=httpx.Response(503))

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, per_request_max_attempts=3, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.FAILED
    assert result.attempts == 3
    assert "503" in (result.error or "")
    assert not (tmp_path / "09999.pdf").exists()
    assert not (tmp_path / "09999.pdf.tmp").exists()


@respx.mock
@pytest.mark.asyncio
async def test_partial_download_then_500_cleans_tmp(tmp_path: Path) -> None:
    """If streaming starts then fails midway, the .tmp must be removed."""
    url = "https://example.test/p.pdf"
    # respx serves a 500 -> _stream_to_tmp raises before opening the file
    # but we explicitly create a stale tmp first to assert it gets cleaned.
    (tmp_path / "09999.pdf.tmp").write_bytes(b"partial-leftover")
    respx.get(url).mock(return_value=httpx.Response(500))

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, per_request_max_attempts=2, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.FAILED
    assert not (tmp_path / "09999.pdf.tmp").exists()


@respx.mock
@pytest.mark.asyncio
async def test_download_many_respects_max_workers(tmp_path: Path) -> None:
    """At most `max_workers` requests are in flight at once."""
    url_template = "https://example.test/{i}.pdf"
    in_flight: list[int] = [0]
    peak: list[int] = [0]

    async def handler(request: httpx.Request) -> httpx.Response:
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        try:
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=PDF_BYTES)
        finally:
            in_flight[0] -= 1

    for i in range(10):
        respx.get(url_template.format(i=i)).mock(side_effect=handler)

    filings = [
        _filing(ticker=f"{i:05d}", url=url_template.format(i=i))
        for i in range(10)
    ]
    dl = PDFDownloader(raw_pdfs_dir=tmp_path, max_workers=3, jitter_seconds=(0.0, 0.0))
    results = await dl.download_many(filings)

    assert len(results) == 10
    assert all(r.is_success for r in results)
    assert peak[0] <= 3


# ---------- default jitter compliance ----------

def test_downloader_default_jitter_matches_spec() -> None:
    dl = PDFDownloader(raw_pdfs_dir=Path("."))
    assert dl.jitter_seconds == (0.3, 0.8)


# ---------- 429 Rate Limiting (spec section 7 retry policy) ----------


def _make_429_response(retry_after: str | None = None) -> httpx.Response:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return httpx.Response(429, headers=headers)


@respx.mock
@pytest.mark.asyncio
async def test_download_429_respects_retry_after_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After: must sleep exactly the header value before retrying."""
    url = "https://example.test/p.pdf"
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("hk_ipo.l0.downloader.asyncio.sleep", fake_sleep)

    respx.get(url).mock(side_effect=[
        _make_429_response("5"),
        _make_429_response("10"),
        httpx.Response(200, content=PDF_BYTES),
    ])

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.SUCCESS
    assert len(sleeps) == 2, f"expected 2 sleeps, got {len(sleeps)}"
    assert sleeps[0] == 5.0, f"first sleep should be 5.0, got {sleeps[0]}"
    assert sleeps[1] == 10.0, f"second sleep should be 10.0, got {sleeps[1]}"


@respx.mock
@pytest.mark.asyncio
async def test_download_429_fallback_backoff_when_no_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 without Retry-After header: fall back to 30s/60s/120s fixed sequence."""
    url = "https://example.test/p.pdf"
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("hk_ipo.l0.downloader.asyncio.sleep", fake_sleep)

    respx.get(url).mock(side_effect=[
        _make_429_response(),  # no header -> 30s
        _make_429_response(),  # no header -> 60s
        httpx.Response(200, content=PDF_BYTES),
    ])

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.SUCCESS
    assert len(sleeps) == 2
    assert sleeps[0] == 30.0, f"first fallback should be 30s, got {sleeps[0]}"
    assert sleeps[1] == 60.0, f"second fallback should be 60s, got {sleeps[1]}"


@respx.mock
@pytest.mark.asyncio
async def test_download_429_exhausts_3_attempts_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 3 consecutive 429s, fail as terminal (no further retries)."""
    url = "https://example.test/p.pdf"
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("hk_ipo.l0.downloader.asyncio.sleep", fake_sleep)

    respx.get(url).mock(side_effect=[
        _make_429_response(),  # attempt 1
        _make_429_response(),  # attempt 2
        _make_429_response(),  # attempt 3 -> terminal
    ])

    dl = PDFDownloader(raw_pdfs_dir=tmp_path, jitter_seconds=(0.0, 0.0))
    result = await dl.download(_filing(url=url))

    assert result.outcome == DownloadOutcome.FAILED
    assert "429" in (result.error or "")
    assert len(sleeps) == 2  # sleeps after attempt 1 (30s) and attempt 2 (60s)
    assert sleeps == [30.0, 60.0]
    assert not (tmp_path / "09999.pdf").exists()
