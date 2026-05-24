"""Unit tests for hk_ipo.l1.source_wikipedia.

Tests cover:
  1. _parse_wikitext extracts tickers, names, dates from wikitable markup
  2. _parse_wikitext skips non-wikitable tables
  3. _parse_wikitext handles missing columns (no date, no company name)
  4. _parse_wikitext handles empty/blank wikitext
  5. fetch_wikipedia uses cache when available
  6. fetch_wikipedia handles force_refresh bypassing cache
  7. fetch_wikipedia graceful failure when all page candidates miss
  8. fetch_wikipedia falls back to second candidate when first fails
  9. fetch_wikipedia ticks all entries have source="wikipedia"
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wikipedia_api_response() -> dict:
    """Load the Wikipedia API JSON fixture."""
    fixture = Path(__file__).parent / "fixtures" / "l1" / "wikipedia_sample.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.fixture
def wikipedia_wikitext(wikipedia_api_response: dict) -> str:
    """Extract wikitext from the fixture for direct parser tests."""
    return wikipedia_api_response["parse"]["wikitext"]["*"]


# ---------------------------------------------------------------------------
# _parse_wikitext: basic parsing
# ---------------------------------------------------------------------------

def test_parse_wikitext_extracts_entries(wikipedia_wikitext: str) -> None:
    """Parser should extract ticker, company name, and date from wikitable markup."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    source_url = "https://en.wikipedia.org/wiki/Test_Page"
    results = _parse_wikitext(wikipedia_wikitext, source_url)

    assert len(results) >= 6, f"Expected at least 6 entries, got {len(results)}"

    # Check a basic entry with standard YYYY-MM-DD date
    quicktron = next(r for r in results if r.hk_ticker == "02556")
    assert quicktron.company_name == "QUICKTRON CO LTD"
    assert quicktron.list_date == date(2025, 1, 15)
    assert quicktron.source == "wikipedia"
    assert quicktron.source_url == source_url

    # Check entry with wiki link
    sinopharm = next(r for r in results if r.hk_ticker == "01099")
    assert "Sinopharm" in sinopharm.company_name
    assert sinopharm.list_date == date(2025, 3, 20)

    # Check entry with DD Month YYYY date format
    cmb = next(r for r in results if r.hk_ticker == "03968")
    assert cmb.company_name == "CHINA MERCHANTS BANK"
    assert cmb.list_date == date(2025, 5, 6)

    # Check entry with YYYY/MM/DD date format
    picc = next(r for r in results if r.hk_ticker == "02328")
    assert picc.company_name == "PICC P&C"
    assert picc.list_date == date(2025, 7, 11)

    # Check entry with Hs template around ticker (in 2024 table)
    zsj = next(r for r in results if r.hk_ticker == "02503")
    assert "Zhongshenjianye" in zsj.company_name
    assert zsj.list_date == date(2024, 1, 9)

    # Check entry with piped wiki link [[Meituan|Meituan Dianping]]
    meituan = next(r for r in results if r.hk_ticker == "03690")
    assert "Meituan" in meituan.company_name
    assert meituan.list_date == date(2024, 9, 20)

    # Check entry with missing date
    missing_date = next(r for r in results if r.hk_ticker == "00123")
    assert missing_date.company_name == "Example Corp (no date)"
    assert missing_date.list_date is None


def test_parse_wikitext_all_entries_have_source(wikipedia_wikitext: str) -> None:
    """Every parsed entry must have source='wikipedia' and a source_url."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    source_url = "https://en.wikipedia.org/wiki/Test_Page"
    results = _parse_wikitext(wikipedia_wikitext, source_url)

    for r in results:
        assert r.source == "wikipedia", f"Expected source 'wikipedia', got '{r.source}'"
        assert r.source_url == source_url, f"Expected source_url '{source_url}'"


def test_parse_wikitext_tickers_are_padded() -> None:
    """Tickers shorter than 5 chars should be zero-padded by ExternalIPO.__post_init__."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """{| class="wikitable"
|-
! Company !! Stock Code !! Listing Date
|-
| SHORT CO || 1 || 2025-01-01
|-
| ANOTHER CO || 09988 || 2025-02-15
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert len(results) == 2
    short_ticker = next(r for r in results if r.company_name == "SHORT CO")
    assert short_ticker.hk_ticker == "00001"
    assert len(short_ticker.hk_ticker) == 5
    long_ticker = next(r for r in results if r.company_name == "ANOTHER CO")
    assert long_ticker.hk_ticker == "09988"


# ---------------------------------------------------------------------------
# _parse_wikitext: skip non-wikitable content
# ---------------------------------------------------------------------------

def test_parse_wikitext_skips_non_wikitable() -> None:
    """Parser should skip tables that don't have class='wikitable'."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    # Only a plain table (no wikitable class) — should be skipped
    wikitext = """{| class="plain"
|-
! Header A !! Header B
|-
| Data A || Data B
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert results == []


def test_parse_wikitext_skips_non_table_content() -> None:
    """Parser should ignore any text outside wikitable blocks."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """==Section==
Some introductory text.
More text here.

{| class="wikitable"
|-
! Col1 !! Col2
|-
| Company || 00123 || 2025-01-01
|}

==Another Section==
More text after table.
"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert len(results) == 1
    assert results[0].hk_ticker == "00123"


# ---------------------------------------------------------------------------
# _parse_wikitext: edge cases
# ---------------------------------------------------------------------------

def test_parse_wikitext_handles_missing_columns() -> None:
    """Parser should handle rows with missing company name or missing date."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """{| class="wikitable"
|-
! Company !! Ticker !! Date
|-
| || 00100 || 2025-01-01
|-
| Company Only || 00200 ||
|-
| Another Company || 00300 || Not a real date
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert len(results) == 3

    # Missing company name → empty string
    no_name = next(r for r in results if r.hk_ticker == "00100")
    assert no_name.company_name == ""
    assert no_name.list_date == date(2025, 1, 1)

    # Missing date → None
    no_date = next(r for r in results if r.hk_ticker == "00200")
    assert no_date.company_name == "Company Only"
    assert no_date.list_date is None

    # Unparseable date → None
    unparseable = next(r for r in results if r.hk_ticker == "00300")
    assert unparseable.company_name == "Another Company"
    assert unparseable.list_date is None


def test_parse_wikitext_handles_empty_wikitext() -> None:
    """Parser returns [] for empty or whitespace-only wikitext."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    assert _parse_wikitext("", "https://example.com") == []
    assert _parse_wikitext("   \n  \n ", "https://example.com") == []


def test_parse_wikitext_handles_no_wikitable() -> None:
    """Parser returns [] when wikitext has no wikitable blocks."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """==Section==
Just some text here. No tables at all.
* Bullet point
* Another bullet
"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert results == []


def test_parse_wikitext_handles_row_with_insufficient_cells() -> None:
    """Parser should skip rows with only one cell (no ticker identifiable)."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """{| class="wikitable"
|-
! Header
|-
| Only one cell with no digits
|-
| CompanyX || 00005 || 2025-05-20
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")
    # Only the third row should produce an entry
    assert len(results) == 1
    assert results[0].hk_ticker == "00005"


def test_parse_wikitext_handles_multiline_rows() -> None:
    """Parser should handle rows where cells are on separate lines."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """{| class="wikitable"
|-
! Company !! Ticker !! Date
|-
| Company A
| 00123
| 2025-01-01
|-
| Company B || 00456 || 2025-06-15
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")
    assert len(results) == 2

    a = next(r for r in results if r.hk_ticker == "00123")
    assert a.company_name == "Company A"
    assert a.list_date == date(2025, 1, 1)

    b = next(r for r in results if r.hk_ticker == "00456")
    assert b.company_name == "Company B"
    assert b.list_date == date(2025, 6, 15)


def test_parse_wikitext_handles_dd_month_yyyy_dates() -> None:
    """Parser should handle various DD Month YYYY date formats."""
    from hk_ipo.l1.source_wikipedia import _parse_wikitext

    wikitext = """{| class="wikitable"
|-
! Company !! Ticker !! Date
|-
| CO A || 00001 || 15 January 2025
|-
| CO B || 00002 || 6 May 2024
|-
| CO C || 00003 || 26 November 2019
|}"""
    results = _parse_wikitext(wikitext, "https://example.com")

    a = next(r for r in results if r.hk_ticker == "00001")
    assert a.list_date == date(2025, 1, 15)

    b = next(r for r in results if r.hk_ticker == "00002")
    assert b.list_date == date(2024, 5, 6)

    c = next(r for r in results if r.hk_ticker == "00003")
    assert c.list_date == date(2019, 11, 26)


# ---------------------------------------------------------------------------
# fetch_wikipedia: caching behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_wikipedia_uses_cache_when_available(
    tmp_path: Path, wikipedia_api_response: dict,
) -> None:
    """fetch_wikipedia should use cached JSON when fresh enough and not force_refresh."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    cache_dir = tmp_path / "data" / "validation" / "raw" / "wikipedia"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "2025.json"
    cache_path.write_text(
        json.dumps(wikipedia_api_response, ensure_ascii=False),
        encoding="utf-8",
    )

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    # Also need an end_year to be current year or specific range
    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=False,
        )

    # Should have results from the cached fixture (6+ entries across 2 tables)
    assert len(results) >= 6
    # No HTTP requests needed — served from cache
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_wikipedia_force_refresh_bypasses_cache(
    tmp_path: Path, wikipedia_api_response: dict,
) -> None:
    """force_refresh=True should skip cache and make HTTP requests."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    # Put stale content in cache
    cache_dir = tmp_path / "data" / "validation" / "raw" / "wikipedia"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "2025.json"
    cache_path.write_text('{"old": "cached"}', encoding="utf-8")

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(
        200,
        json=wikipedia_api_response,
    ))

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=True,
        )

    assert len(results) >= 6
    assert mock_client.get.call_count >= 1


# ---------------------------------------------------------------------------
# fetch_wikipedia: error handling and candidate fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_wikipedia_graceful_failure_all_candidates_miss(
    tmp_path: Path,
) -> None:
    """fetch_wikipedia should return [] when all page-name candidates fail."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    # Wikipedia returns error for both candidates
    error_response = {"error": {"code": "missingtitle", "info": "Page not found."}}

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json=error_response))

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=True,
        )

    assert results == []
    # Should have tried both candidates
    assert mock_client.get.call_count == 2

    # Verify error was logged
    error_log = tmp_path / "data" / "validation" / "source_errors.json"
    assert error_log.exists()
    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["source"] == "wikipedia"
    assert "2025" in entries[0]["reason"]


@pytest.mark.asyncio
async def test_fetch_wikipedia_falls_back_to_second_candidate(
    tmp_path: Path, wikipedia_api_response: dict,
) -> None:
    """When first page-name candidate fails, should try the second candidate."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    error_response = {"error": {"code": "missingtitle", "info": "Page not found."}}

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=[
        httpx.Response(200, json=error_response),  # first candidate fails
        httpx.Response(200, json=wikipedia_api_response),  # second succeeds
    ])

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=True,
        )

    assert len(results) >= 6
    assert mock_client.get.call_count == 2

    # Cache should be written for the successful candidate
    cache_path = tmp_path / "data" / "validation" / "raw" / "wikipedia" / "2025.json"
    assert cache_path.exists()


@pytest.mark.asyncio
async def test_fetch_wikipedia_handles_http_error(tmp_path: Path) -> None:
    """fetch_wikipedia should handle HTTP errors gracefully across candidates."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=True,
        )

    assert results == []


@pytest.mark.asyncio
async def test_fetch_wikipedia_handles_non_200_status(tmp_path: Path) -> None:
    """Non-200 HTTP responses should cause candidates to be skipped."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock(return_value=httpx.Response(503, text="Service Unavailable"))

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2025,
            end_year=2025,
            force_refresh=True,
        )

    assert results == []


@pytest.mark.asyncio
async def test_fetch_wikipedia_multiple_years(
    tmp_path: Path, wikipedia_api_response: dict,
) -> None:
    """fetch_wikipedia should iterate over the year range and aggregate results."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    # Cache for 2024 and 2025 already present
    cache_dir = tmp_path / "data" / "validation" / "raw" / "wikipedia"
    cache_dir.mkdir(parents=True)
    (cache_dir / "2024.json").write_text(
        json.dumps(wikipedia_api_response, ensure_ascii=False),
        encoding="utf-8",
    )
    (cache_dir / "2025.json").write_text(
        json.dumps(wikipedia_api_response, ensure_ascii=False),
        encoding="utf-8",
    )

    mock_client = MagicMock(spec=ValidationHTTPClient)
    mock_client.get = AsyncMock()

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=2024,
            end_year=2025,
            force_refresh=False,
        )

    # Both years cached → results should be roughly 2x the single-year count
    assert len(results) >= 12
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_wikipedia_default_end_year(
    tmp_path: Path, wikipedia_api_response: dict,
) -> None:
    """When end_year is None, it should default to the current calendar year."""
    from hk_ipo.l1._http import ValidationHTTPClient
    from hk_ipo.l1.source_wikipedia import fetch_wikipedia

    mock_client = MagicMock(spec=ValidationHTTPClient)
    # Return error for all years except the cached ones
    error_response = {"error": {"code": "missingtitle"}}
    mock_client.get = AsyncMock(return_value=httpx.Response(200, json=error_response))

    from datetime import datetime
    current_year = datetime.now().year

    with patch("hk_ipo.l1.source_wikipedia._cache_dir") as mock_cache_dir:
        mock_cache_dir.return_value = tmp_path / "data" / "validation"

        results = await fetch_wikipedia(
            mock_client,
            start_year=current_year + 100,  # far future → no years to iterate
            force_refresh=True,
        )

    # start_year > end_year → no range to iterate
    assert results == []


# ---------------------------------------------------------------------------
# _log_source_error
# ---------------------------------------------------------------------------

def test_log_source_error_creates_file_and_appends(tmp_path: Path) -> None:
    """_log_source_error creates the JSON file and appends entries."""
    from hk_ipo.l1.source_wikipedia import _log_source_error

    error_log = tmp_path / "data" / "validation" / "source_errors.json"

    with patch("hk_ipo.l1.source_wikipedia._error_log_path") as mock_path:
        mock_path.return_value = error_log

        _log_source_error("Test error 1")
        _log_source_error("Test error 2")

    assert error_log.exists()
    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 2
    assert entries[0]["source"] == "wikipedia"
    assert entries[0]["reason"] == "Test error 1"
    assert entries[1]["reason"] == "Test error 2"
    assert "timestamp" in entries[0]


def test_log_source_error_enforces_50_entry_cap(tmp_path: Path) -> None:
    """_log_source_error keeps only the last 50 entries."""
    from hk_ipo.l1.source_wikipedia import _log_source_error

    error_log = tmp_path / "data" / "validation" / "source_errors.json"

    with patch("hk_ipo.l1.source_wikipedia._error_log_path") as mock_path:
        mock_path.return_value = error_log

        for i in range(55):
            _log_source_error(f"Error {i}")

    entries = json.loads(error_log.read_text(encoding="utf-8"))
    assert len(entries) == 50
    assert entries[0]["reason"] == "Error 5"
    assert entries[-1]["reason"] == "Error 54"
