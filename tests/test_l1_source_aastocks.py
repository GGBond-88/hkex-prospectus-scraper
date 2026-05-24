"""Unit tests for hk_ipo.l1.source_aastocks.

Tests cover:
  1. _parse_aastocks_page extracts ticker, name, list_date correctly from server-rendered HTML
  2. _parse_aastocks_page handles empty pages (no IPO rows)
  3. _parse_aastocks_page skips rows missing ticker data
  4. _parse_aastocks_page allows missing company name / null list_date
  5. _parse_aastocks_page returns [] on blank/whitespace HTML
  6. fetch_aastocks uses cache when available
  7. fetch_aastocks pagination walk stops on empty page
  8. fetch_aastocks handles HTTP errors gracefully
  9. fetch_aastocks respects max_pages safety limit
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_html() -> str:
    """Load the AAStocks sample HTML fixture."""
    fixture = Path(__file__).parent / "fixtures" / "l1" / "aastocks_sample.html"
    return fixture.read_text(encoding="utf-8")


@pytest.fixture
def empty_html() -> str:
    """Load the AAStocks empty page fixture."""
    fixture = Path(__file__).parent / "fixtures" / "l1" / "aastocks_empty.html"
    return fixture.read_text(encoding="utf-8")


@pytest.fixture
def page2_empty_html() -> str:
    """Load the AAStocks empty page 2 fixture (for pagination stop test)."""
    fixture = Path(__file__).parent / "fixtures" / "l1" / "aastocks_page2_empty.html"
    return fixture.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_aastocks_page: parsing well-formed tables
# ---------------------------------------------------------------------------

def test_parse_aastocks_page_extracts_well_formed_table(sample_html: str) -> None:
    """Parser should extract ticker, company name, and date from server-rendered
    AAStocks IPO table."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    source_url = "http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx?s=1&o=0&page=1"
    results = _parse_aastocks_page(sample_html, source_url)

    assert len(results) >= 3

    # Check ZIJIN GOLD INTL (02259)
    zijin = next(r for r in results if r.hk_ticker == "02259")
    assert zijin.company_name == "ZIJIN GOLD INTL"
    assert zijin.list_date == date(2025, 9, 30)
    assert zijin.source == "aastocks"
    assert zijin.source_url == source_url

    # Check ZHOU LIU FU (06168)
    zlf = next(r for r in results if r.hk_ticker == "06168")
    assert zlf.company_name == "ZHOU LIU FU"
    assert zlf.list_date == date(2025, 6, 26)

    # Check ZHONGSHENJIANYE (02503)
    zsj = next(r for r in results if r.hk_ticker == "02503")
    assert zsj.company_name == "ZHONGSHENJIANYE"
    assert zsj.list_date == date(2024, 1, 9)


def test_parse_aastocks_page_all_entries_have_source(sample_html: str) -> None:
    """Every parsed entry must have source='aastocks' and a source_url."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    source_url = "http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx?s=1&o=0&page=1"
    results = _parse_aastocks_page(sample_html, source_url)

    for r in results:
        assert r.source == "aastocks"
        assert r.source_url == source_url


def test_parse_aastocks_page_tickers_are_padded() -> None:
    """Tickers shorter than 5 chars should be zero-padded by ExternalIPO.__post_init__."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    # Use inline HTML with a short ticker "1.HK" to verify padding
    html = """<html><body>
<div id="IPOListed">
<table class="ns2 dataTable">
<thead><tr><td></td><td>Name/Code</td><td>Listing Date</td></tr></thead>
<tbody>
<tr>
<td></td>
<td class="txt_l"><a href="/summary">Short Ticker Co</a><br/><a class="cls" href="/quote">1.HK</a></td>
<td class="txt_r">2025/03/15</td>
</tr>
</tbody>
</table>
</div>
</body></html>"""
    source_url = "http://example.com"
    results = _parse_aastocks_page(html, source_url)

    assert len(results) == 1
    assert results[0].hk_ticker == "00001", (
        f"Expected padded ticker '00001', got '{results[0].hk_ticker}'"
    )
    assert len(results[0].hk_ticker) == 5


# ---------------------------------------------------------------------------
# _parse_aastocks_page: handling empty/missing data
# ---------------------------------------------------------------------------

def test_parse_aastocks_page_returns_empty_list_on_empty_page(empty_html: str) -> None:
    """Parser returns [] when the page has no IPO data rows."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    results = _parse_aastocks_page(empty_html, "http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx?s=1&o=0&page=1")
    assert results == []


def test_parse_aastocks_page_returns_empty_list_on_blank_html() -> None:
    """Parser returns [] for blank/whitespace-only HTML."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    results = _parse_aastocks_page("", "http://example.com")
    assert results == []


def test_parse_aastocks_page_returns_empty_list_on_no_table() -> None:
    """Parser returns [] when HTML has no dataTable table."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    html = "<html><body><p>No tables here</p></body></html>"
    results = _parse_aastocks_page(html, "http://example.com")
    assert results == []


def test_parse_aastocks_page_skips_row_without_ticker() -> None:
    """Rows without a valid stock code link should be skipped."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    html = """<html><body>
<div id="IPOListed">
<table class="ns2 dataTable">
<thead><tr><td></td><td>Name/Code</td><td>Listing Date</td></tr></thead>
<tbody>
<tr>
<td></td>
<td class="txt_l">Just text, no link</td>
<td class="txt_r">2025/01/01</td>
</tr>
<tr>
<td></td>
<td class="txt_l"><a href="/summary">Some Company</a><br/><a class="cls" href="/quote">00001.HK</a></td>
<td class="txt_r">2025/01/01</td>
</tr>
</tbody>
</table>
</div>
</body></html>"""
    results = _parse_aastocks_page(html, "http://example.com")
    assert len(results) == 1
    assert results[0].hk_ticker == "00001"


def test_parse_aastocks_page_handles_missing_company_name() -> None:
    """Rows with ticker but no company name <a> should have empty string name."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    html = """<html><body>
<div id="IPOListed">
<table class="ns2 dataTable">
<thead><tr><td></td><td>Name/Code</td><td>Listing Date</td></tr></thead>
<tbody>
<tr>
<td></td>
<td class="txt_l"><a class="cls" href="/quote">00002.HK</a></td>
<td class="txt_r">2025/03/15</td>
</tr>
</tbody>
</table>
</div>
</body></html>"""
    results = _parse_aastocks_page(html, "http://example.com")
    assert len(results) == 1
    assert results[0].hk_ticker == "00002"
    assert results[0].company_name == ""
    assert results[0].list_date == date(2025, 3, 15)


def test_parse_aastocks_page_handles_missing_date() -> None:
    """Rows with ticker but no parsable date should have list_date=None."""
    from hk_ipo.l1.source_aastocks import _parse_aastocks_page

    html = """<html><body>
<div id="IPOListed">
<table class="ns2 dataTable">
<thead><tr><td></td><td>Name/Code</td><td>Listing Date</td></tr></thead>
<tbody>
<tr>
<td></td>
<td class="txt_l"><a href="/summary">Test Co</a><br/><a class="cls" href="/quote">00003.HK</a></td>
<td class="txt_r">N/A</td>
</tr>
</tbody>
</table>
</div>
</body></html>"""
    results = _parse_aastocks_page(html, "http://example.com")
    assert len(results) == 1
    assert results[0].hk_ticker == "00003"
    assert results[0].company_name == "Test Co"
    assert results[0].list_date is None


# ---------------------------------------------------------------------------
# fetch_aastocks: caching behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_aastocks_uses_cache_when_available(
    tmp_path: Path, sample_html: str, empty_html: str,
) -> None:
    """fetch_aastocks should use cached file when fresh enough and not force_refresh."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    cache_dir = tmp_path / "data" / "validation" / "raw" / "aastocks"
    cache_dir.mkdir(parents=True)
    (cache_dir / "page_1.html").write_text(sample_html, encoding="utf-8")
    # Also cache page 2 as empty to stop pagination without HTTP
    (cache_dir / "page_2.html").write_text(empty_html, encoding="utf-8")

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=False,
        )

    # Should have 3 results from page 1 cache (page 2 cached empty → stop)
    assert len(results) == 3
    # No HTTP requests needed — both pages served from cache
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_aastocks_force_refresh_bypasses_cache(
    tmp_path: Path, sample_html: str, page2_empty_html: str,
) -> None:
    """force_refresh=True should skip cache and make HTTP requests."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    cache_dir = tmp_path / "data" / "validation" / "raw" / "aastocks"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "page_1.html"
    cache_file.write_text("old cached content", encoding="utf-8")

    mock_client = MagicMock(spec=ValidationHTTPClient)
    # Page 1 returns sample data, page 2 returns empty (pagination stops)
    mock_client.get = AsyncMock(side_effect=[
        httpx.Response(200, text=sample_html),
        httpx.Response(200, text=page2_empty_html),
    ])

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=True,
        )

    assert len(results) == 3
    assert mock_client.get.call_count == 2  # page 1 + page 2 (empty -> stop)


# ---------------------------------------------------------------------------
# fetch_aastocks: pagination behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_aastocks_pagination_stops_on_empty_page(
    tmp_path: Path, sample_html: str, page2_empty_html: str,
) -> None:
    """Pagination should stop when a page returns no IPO rows."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=[
        httpx.Response(200, text=sample_html),
        httpx.Response(200, text=page2_empty_html),
    ])

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=True,
        )

    assert len(results) == 3
    # Should have fetched page 1 and page 2 (then stopped on empty page 2)
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_aastocks_respects_max_pages(
    tmp_path: Path, sample_html: str,
) -> None:
    """Should not exceed max_pages even if server keeps returning data."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    mock_client = MagicMock(spec=ValidationHTTPClient)
    # Return sample HTML for all 3 calls (nominally would be 3 separate pages)
    mock_client.get = AsyncMock(return_value=httpx.Response(200, text=sample_html))

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            max_pages=2,
            force_refresh=True,
        )

    # 2 pages x 3 rows per page = 6 entries
    assert len(results) == 6
    assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# fetch_aastocks: error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_aastocks_handles_http_error(tmp_path: Path) -> None:
    """fetch_aastocks should handle HTTP errors gracefully and return []."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=True,
        )

    assert results == []


@pytest.mark.asyncio
async def test_fetch_aastocks_handles_non_200_status(
    tmp_path: Path,
) -> None:
    """Non-200 HTTP responses raise an exception, causing the orchestrator to skip the
    page and continue to the next. The error is logged to source_errors.json."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(
        503,
        text="Service Unavailable",
    ))

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=True,
            max_pages=2,
        )

    # Both pages returned non-200, so all were skipped → no results.
    assert results == []
    assert mock_client.get.call_count == 2
    # Verify error was logged for both pages.
    import json
    error_log = tmp_path / "data" / "validation" / "source_errors.json"
    assert error_log.exists()
    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_fetch_aastocks_pagination_continues_after_http_error(
    tmp_path: Path, sample_html: str,
) -> None:
    """If page 1 fails but page 2 succeeds, should still get page 2 results."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_aastocks import fetch_aastocks

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=[
        httpx.ConnectError("Connection refused"),
        httpx.Response(200, text=sample_html),
        httpx.Response(200, text="<html>No related information.</html>"),
    ])

    with patch("hk_ipo.l1.source_aastocks._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_aastocks(
            mock_client,
            force_refresh=True,
        )

    # Page 1 failed, page 2 returned 3 results, page 3 empty -> stop
    assert len(results) == 3


# ---------------------------------------------------------------------------
# _log_source_error
# ---------------------------------------------------------------------------

def test_log_source_error_creates_file_and_appends(tmp_path: Path) -> None:
    """_log_source_error creates the JSON file and appends entries."""
    import json
    from hk_ipo.l1.source_aastocks import _log_source_error

    error_log = tmp_path / "data" / "validation" / "source_errors.json"

    with patch("hk_ipo.l1.source_aastocks._error_log_path") as mock_path:
        mock_path.return_value = error_log

        _log_source_error("Test error 1")
        _log_source_error("Test error 2")

    assert error_log.exists()
    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 2
    assert entries[0]["source"] == "aastocks"
    assert entries[0]["reason"] == "Test error 1"
    assert entries[1]["reason"] == "Test error 2"
    assert "timestamp" in entries[0]


def test_log_source_error_enforces_50_entry_cap(tmp_path: Path) -> None:
    """_log_source_error keeps only the last 50 entries."""
    import json
    from hk_ipo.l1.source_aastocks import _log_source_error

    error_log = tmp_path / "data" / "validation" / "source_errors.json"

    with patch("hk_ipo.l1.source_aastocks._error_log_path") as mock_path:
        mock_path.return_value = error_log

        # Write 55 entries
        for i in range(55):
            _log_source_error(f"Error {i}")

    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 50
    # Should keep the last 50 (entries 5-54)
    assert entries[0]["reason"] == "Error 5"
    assert entries[-1]["reason"] == "Error 54"


def test_log_source_error_handles_corrupt_json(tmp_path: Path) -> None:
    """_log_source_error recovers gracefully from a corrupt JSON file."""
    import json
    from hk_ipo.l1.source_aastocks import _log_source_error

    error_log = tmp_path / "data" / "validation" / "source_errors.json"
    error_log.parent.mkdir(parents=True, exist_ok=True)
    error_log.write_text("this is not valid json", encoding="utf-8")

    with patch("hk_ipo.l1.source_aastocks._error_log_path") as mock_path:
        mock_path.return_value = error_log

        _log_source_error("Recovery error")

    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["reason"] == "Recovery error"
