"""Unit tests for hk_ipo.l1.source_hkex_stats.

Tests cover:
  1. _parse_hkex_page extracts tickers, names, and dates from a well-formed table
  2. _parse_hkex_page handles missing ticker, name, and date fields gracefully
  3. _parse_hkex_page returns empty list on HTML with no IPO data
  4. fetch_hkex_stats returns [] — skeleton mode (early return in _fetch_year)
  5. fetch_hkex_stats handles start_year > end_year
  6. fetch_hkex_stats does not crash on HTTP errors
"""
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
    """Load the HKEX stats HTML fixture."""
    fixture = Path(__file__).parent / "fixtures" / "l1" / "hkex_stats_sample.html"
    return fixture.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_hkex_page: parsing well-formed tables
# ---------------------------------------------------------------------------

def test_parse_hkex_page_extracts_well_formed_table(sample_html: str) -> None:
    """Parser should extract ticker, company name, and date from a well-formed
    newly-listed-companies table."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://www.hkex.com.hk/Market-Data/Statistics/sample"
    results = _parse_hkex_page(sample_html, source_url)

    assert len(results) >= 3

    # Check Xiaomi (01810) is parsed correctly
    xiaomi = next(r for r in results if r.hk_ticker == "01810")
    assert xiaomi.company_name == "Xiaomi Corporation"
    assert xiaomi.list_date == date(2018, 7, 9)
    assert xiaomi.source == "hkex_stats"
    assert xiaomi.source_url == source_url

    # Check Horizon Robotics (09660)
    horizon = next(r for r in results if r.hk_ticker == "09660")
    assert horizon.company_name == "Horizon Robotics"
    assert horizon.list_date == date(2024, 10, 24)

    # Check Lianlian DigiTech (02598)
    lianlian = next(r for r in results if r.hk_ticker == "02598")
    assert lianlian.company_name == "Lianlian DigiTech Co., Ltd."
    assert lianlian.list_date == date(2024, 6, 28)


def test_parse_hkex_page_all_entries_have_source(sample_html: str) -> None:
    """Every parsed entry must have source='hkex_stats' and a source_url."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    for r in results:
        assert r.source == "hkex_stats"
        assert r.source_url == source_url


def test_parse_hkex_page_tickers_are_padded(sample_html: str) -> None:
    """All tickers should be zero-padded to 5 characters."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    for r in results:
        assert len(r.hk_ticker) == 5, f"{r.hk_ticker} should be 5-char padded"
        assert r.hk_ticker == r.hk_ticker.zfill(5)


# ---------------------------------------------------------------------------
# _parse_hkex_page: handling missing fields
# ---------------------------------------------------------------------------

def test_parse_hkex_page_skips_row_with_missing_ticker(sample_html: str) -> None:
    """Rows with empty ticker should be skipped."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    # No company named "No Ticker Co" should appear
    names = {r.company_name for r in results}
    assert "No Ticker Co" not in names


def test_parse_hkex_page_allows_missing_company_name(sample_html: str) -> None:
    """Rows with ticker + date but missing name should still be parsed,
    with company_name set to empty string."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    # 00005 is in the partial-data-table with empty company name but has a date
    ticker_00005 = [r for r in results if r.hk_ticker == "00005"]
    assert len(ticker_00005) == 1
    assert ticker_00005[0].company_name == ""
    assert ticker_00005[0].list_date == date(2024, 2, 1)


def test_parse_hkex_page_allows_missing_list_date(sample_html: str) -> None:
    """Rows with ticker + name but missing date should still be parsed,
    with list_date=None."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    # 00011 is in the partial-data-table with empty date but has a name
    ticker_00011 = [r for r in results if r.hk_ticker == "00011"]
    assert len(ticker_00011) == 1
    assert ticker_00011[0].list_date is None
    assert ticker_00011[0].company_name == "Valid Co"


# ---------------------------------------------------------------------------
# _parse_hkex_page: non-IPO content
# ---------------------------------------------------------------------------

def test_parse_hkex_page_returns_empty_list_on_empty_html() -> None:
    """Parser should return [] for HTML that has no IPO data tables."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    html = "<html><body><p>No tables here</p></body></html>"
    results = _parse_hkex_page(html, "https://example.com")
    assert results == []


def test_parse_hkex_page_returns_empty_list_on_whitespace() -> None:
    """Parser should handle empty/whitespace-only HTML gracefully."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    results = _parse_hkex_page("", "https://example.com")
    assert results == []


def test_parse_hkex_page_does_not_parse_non_ipo_tables(sample_html: str) -> None:
    """Market statistics rows (turnover, market cap) should not produce IPO entries."""
    from hk_ipo.l1.source_hkex_stats import _parse_hkex_page

    source_url = "https://hkex.com/sample"
    results = _parse_hkex_page(sample_html, source_url)

    # The market stats table has "Month", "Turnover (HKD)", etc. — not IPOs
    # No result should have "January" as company name
    company_names = {r.company_name for r in results}
    assert "January" not in company_names


# ---------------------------------------------------------------------------
# fetch_hkex_stats: skeleton behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hkex_stats_start_year_gt_end_year_returns_empty(
    tmp_path: Path,
) -> None:
    """When start_year > end_year, fetch_hkex_stats should log a warning and
    return [] without making any HTTP requests."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    results = await fetch_hkex_stats(
        mock_client,
        start_year=2025,
        end_year=2020,
    )
    assert results == []
    mock_client.get.assert_not_called()

@pytest.mark.asyncio
async def test_fetch_hkex_stats_returns_empty_list(tmp_path: Path) -> None:
    """When HKEX page has no individual IPO data, fetch_hkex_stats returns []."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    # Mock the HTTP client to return a typical HKEX page (no IPO table data)
    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(
        200,
        text="<html><body><h1>HKEX Monthly Market Highlights</h1>"
             "<p>49 newly listed companies YTD</p></body></html>",
    ))

    results = await fetch_hkex_stats(
        mock_client,
        start_year=2024,
        force_refresh=True,
    )
    assert isinstance(results, list)
    assert results == []


@pytest.mark.asyncio
async def test_fetch_hkex_stats_does_not_crash_on_http_error(tmp_path: Path) -> None:
    """fetch_hkex_stats should handle HTTP errors gracefully — log and return []."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    results = await fetch_hkex_stats(
        mock_client,
        start_year=2024,
        force_refresh=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_fetch_hkex_stats_skeleton_does_not_write_cache(
    tmp_path: Path,
) -> None:
    """In skeleton mode, _fetch_year returns [] early — no HTTP requests and
    therefore no cache files are written."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    with patch("hk_ipo.l1.source_hkex_stats._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"
        results = await fetch_hkex_stats(
            mock_client,
            start_year=2024,
            force_refresh=True,
        )

    assert results == []
    # No HTTP requests were made, so no cache files should exist.
    cache_files = list(tmp_path.rglob("*.html"))
    assert len(cache_files) == 0
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_hkex_stats_skeleton_returns_empty_without_http(
    tmp_path: Path,
) -> None:
    """In skeleton mode, _fetch_year returns [] early regardless of whether
    a cache file exists — no HTTP requests are made."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    cache_dir = tmp_path / "data" / "validation" / "raw" / "hkex_stats"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "2024.html"
    cache_file.write_text(
        "<html><body><h1>HKEX Monthly Market Highlights</h1></body></html>",
        encoding="utf-8",
    )

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    # Patch the cache path to use tmp_path
    with patch("hk_ipo.l1.source_hkex_stats._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_hkex_stats(
            mock_client,
            start_year=2024,
            end_year=2024,
            force_refresh=False,
        )

    # Should return [] (skeleton behavior)
    assert results == []
    # Should NOT have called client.get (used cache)
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_hkex_stats_force_refresh_returns_empty(
    tmp_path: Path,
) -> None:
    """In skeleton mode, force_refresh is irrelevant — _fetch_year returns []
    immediately without making HTTP requests."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    cache_dir = tmp_path / "data" / "validation" / "raw" / "hkex_stats"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "2024.html"
    cache_file.write_text("<html>Cached content</html>", encoding="utf-8")

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    with patch("hk_ipo.l1.source_hkex_stats._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_hkex_stats(
            mock_client,
            start_year=2024,
            end_year=2024,
            force_refresh=True,
        )

    # Skeleton returns [] without calling client.get.
    mock_client.get.assert_not_called()
    assert results == []


@pytest.mark.asyncio
async def test_fetch_hkex_stats_default_end_year_is_current(tmp_path: Path) -> None:
    """When end_year is None, it should default to the current year."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    from datetime import datetime

    current_year = datetime.now().year

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(
        200,
        text="<html>Content</html>",
    ))

    with patch("hk_ipo.l1.source_hkex_stats._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        # Should not raise; end_year defaults to current year
        results = await fetch_hkex_stats(
            mock_client,
            start_year=2020,
            force_refresh=True,
        )

    assert results == []


@pytest.mark.asyncio
async def test_fetch_hkex_stats_handles_non_200_status(tmp_path: Path) -> None:
    """Non-200 HTTP responses should be logged and result in empty list."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_hkex_stats import fetch_hkex_stats

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(
        404,
        text="Not Found",
    ))

    with patch("hk_ipo.l1.source_hkex_stats._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_hkex_stats(
            mock_client,
            start_year=2024,
            force_refresh=True,
        )

    assert results == []
