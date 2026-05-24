"""HKEX official new-listing statistics source.

Fetches HKEX's Monthly Market Highlights page and attempts to extract
individual IPO entries (ticker, company name, listing date).

IMPORTANT LIMITATION (2026-05-24):
    The HKEX Monthly Market Highlights page
    (https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/HKEX-Monthly-Market-Highlights)
    is a JS-heavy ASP.NET/Sitecore page that only presents **aggregate**
    statistics (counts, totals) in server-rendered HTML. Individual IPO
    data (stock code + company name + listing date) is NOT available in
    the server-rendered markup.

    The HKEX All Securities List XLSX
    (https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx)
    provides stock codes and names but does NOT include listing dates,
    making it impossible to filter for newly-listed companies within a
    given year range.

    Consequently, this module is a **documented skeleton**: it fetches
    and caches the page for future inspection, logs the limitation to
    data/validation/source_errors.json, and returns an empty list.

    When HKEX provides a machine-readable feed for newly-listed companies,
    this module should be upgraded to parse it. Candidates:
      - HKEX News feed (https://www.hkexnews.hk) — listing announcements
        may be parseable with SCMP / EDGAR-like logic
      - A third-party aggregator with HKEX-provenance data

This source returns an empty list by design per the graceful-degradation
requirement in the spec (two-source agreement still functions with
sources B and C — aastocks and wikipedia).
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

from selectolax.parser import HTMLParser

from hk_ipo.l1._http import ValidationHTTPClient, should_use_cache
from hk_ipo.l1.models import ExternalIPO

logger = logging.getLogger("hk_ipo")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_NAME = "hkex_stats"
HKEX_MONTHLY_HIGHLIGHTS_URL = (
    "https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/"
    "HKEX-Monthly-Market-Highlights?sc_lang=en"
)
# Explanation for why this source returns no data.
# Guard to ensure the skeleton reason is logged at most once per process.
_SKELETON_LOGGED = False

_SKELETON_REASON = (
    "HKEX Monthly Market Highlights page is JS-heavy and contains only "
    "aggregate statistics (counts, totals). Individual IPO data (stock "
    "code, company name, listing date) is not available in server-rendered "
    "HTML. The HKEX All Securities List XLSX has stock codes and names "
    "but no listing dates. See source_hkex_stats.py docstring for details."
)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    """Return the root cache directory for validation data."""
    return Path("data/validation")


def _error_log_path() -> Path:
    """Return the path to source_errors.json."""
    path = _cache_dir() / "source_errors.json"
    return path


def _log_source_error(reason: str) -> None:
    """Append a timestamped error entry to source_errors.json.

    Creates the file and parent directories if they do not exist.
    """
    log_path = _error_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "source": SOURCE_NAME,
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
    }

    existing: list[dict] = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(entry)

    # Keep only the last 50 entries to avoid unbounded growth.
    existing = existing[-50:]

    log_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

async def fetch_hkex_stats(
    client: ValidationHTTPClient,
    start_year: int = 2010,
    end_year: int | None = None,
    *,
    force_refresh: bool = False,
) -> list[ExternalIPO]:
    """Fetch HKEX new-listing statistics. Caches raw responses.

    IMPORTANT: Currently returns an empty list because HKEX does not
    provide individual-IPO-level data in a machine-readable format.
    See module docstring for the full explanation.

    Returns:
        list[ExternalIPO] with source="hkex_stats". Currently always [].
        On failure, logs to source_errors.json and returns [].
    """
    if end_year is None:
        end_year = datetime.now().year

    # Validate year range
    if start_year > end_year:
        logger.warning(
            "start_year (%d) > end_year (%d) for hkex_stats; returning []",
            start_year,
            end_year,
        )
        return []

    # Collect results per-year.
    # NOTE: HKEX_MONTHLY_HIGHLIGHTS_URL is NOT year-parameterized — every
    # iteration would fetch the same URL.  _fetch_year returns [] immediately
    # in skeleton mode.  When HKEX provides a per-year endpoint, parameterize
    # the URL here (e.g. with a ?year=<year> query parameter).
    all_results: list[ExternalIPO] = []

    for year in range(start_year, end_year + 1):
        try:
            year_results = await _fetch_year(client, year, force_refresh=force_refresh)
            all_results.extend(year_results)
        except Exception:
            logger.exception(
                "Failed to fetch hkex_stats data for year %d; skipping.", year
            )
            _log_source_error(
                f"Exception while fetching HKEX stats for year {year}"
            )

    # Log the skeleton reason once per session.
    global _SKELETON_LOGGED
    if not all_results and not _SKELETON_LOGGED:
        _SKELETON_LOGGED = True
        logger.info(
            "hkex_stats source returned 0 results. %s",
            _SKELETON_REASON,
        )
        _log_source_error(_SKELETON_REASON)

    return all_results


# ---------------------------------------------------------------------------
# Per-year fetch
# ---------------------------------------------------------------------------

async def _fetch_year(
    client: ValidationHTTPClient,
    year: int,
    *,
    force_refresh: bool = False,
) -> list[ExternalIPO]:
    """Fetch and parse HKEX data for a single year.

    Caches the raw HTML under:
        data/validation/raw/hkex_stats/<year>.html

    SKELETON: The HKEX Monthly Market Highlights page does not contain
    individual IPO data (only aggregate statistics).  The URL is NOT
    year-parameterized, so every call would fetch the same page.  We
    return [] immediately without making HTTP requests.  When HKEX
    provides a per-year endpoint or machine-readable feed, restore the
    HTTP + parsing logic below.
    """
    # SKELETON early return — no IPO data is available from this page.
    return []

    cache_dir = _cache_dir() / "raw" / SOURCE_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{year}.html"

    source_url = HKEX_MONTHLY_HIGHLIGHTS_URL

    # -- read from cache if fresh enough --------------------------------
    if should_use_cache(cache_path, force_refresh=force_refresh):
        logger.debug("Using cached HKEX stats for year %d: %s", year, cache_path)
        html = cache_path.read_text(encoding="utf-8")
        return _parse_hkex_page(html, source_url)

    # -- fetch from network ---------------------------------------------
    try:
        resp = await client.get(source_url)
    except Exception:
        logger.exception("HTTP request failed for HKEX stats (year %d)", year)
        return []

    if resp.status_code != 200:
        logger.warning(
            "HKEX stats page returned HTTP %d for year %d; caching raw response.",
            resp.status_code,
            year,
        )
        cache_path.write_text(resp.text, encoding="utf-8")
        return []

    # Cache the successful response
    cache_path.write_text(resp.text, encoding="utf-8")

    return _parse_hkex_page(resp.text, source_url)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_hkex_page(html: str, source_url: str) -> list[ExternalIPO]:
    """Parse an HKEX statistics page into IPO entries.

    Uses selectolax to find table rows with individual company listings.
    Currently, the real HKEX page does NOT contain such tables (only
    aggregate statistics). This parser is designed to extract data from
    hypothetical or future page structures and is unit-tested against
    fixture HTML.

    Parsing logic:
      1. Find tables with candidate headers ("stock code", "company", "listing date")
      2. Extract rows with 3+ cells
      3. Skip rows where the first cell is empty (no ticker)
      4. Build ExternalIPO objects with source="hkex_stats"

    Args:
        html: The raw HTML content.
        source_url: Provenance URL for the data.

    Returns:
        list[ExternalIPO] (may be empty).
    """
    if not html or not html.strip():
        return []

    tree = HTMLParser(html)
    results: list[ExternalIPO] = []

    for table_node in tree.css("table"):
        # Determine if this table contains IPO data by inspecting headers.
        headers = _extract_table_headers(table_node)
        if not _is_ipo_table(headers):
            continue

        # Parse data rows.
        rows = table_node.css("tbody tr")
        if not rows:
            rows = table_node.css("tr")

        for row in rows:
            cells = row.css("td, th")
            if len(cells) < 3:
                continue

            ticker_raw = _cell_text(cells[0])
            company_raw = _cell_text(cells[1])
            date_raw = _cell_text(cells[2])

            # Skip header rows that reuse <th> inside <tbody>
            if _is_header_text(ticker_raw):
                continue

            # Must have at minimum a ticker to be a valid row.
            ticker = ticker_raw.strip()
            if not ticker or not ticker.isdigit():
                continue

            company = company_raw.strip() if company_raw.strip() else ""
            list_date = _parse_date(date_raw.strip())

            results.append(
                ExternalIPO(
                    hk_ticker=ticker,
                    company_name=company,
                    list_date=list_date,
                    source=SOURCE_NAME,
                    source_url=source_url,
                )
            )

    return results


def _extract_table_headers(table_node) -> list[str]:
    """Extract header text from a table node."""
    headers: list[str] = []
    for th in table_node.css("thead th, thead td, th"):
        text = _cell_text(th).strip().lower()
        if text:
            headers.append(text)
    return headers


def _is_ipo_table(headers: list[str]) -> bool:
    """Heuristic: a table is an IPO listing table if it has headers
    referencing stock code, company name, and listing date."""
    header_text = " ".join(headers)
    has_stock = any(kw in header_text for kw in ("stock code", "code"))
    has_company = any(
        kw in header_text for kw in ("company", "name of securities", "issuer")
    )
    has_date = any(
        kw in header_text for kw in ("list", "listing date", "date")
    )
    return has_stock and (has_company or has_date)


def _is_header_text(text: str) -> bool:
    """Return True if the text looks like a table header rather than data."""
    lowered = text.strip().lower()
    header_keywords = [
        "stock code", "company", "name of securities", "listing date",
        "month", "turnover", "market cap", "category", "sub-category",
    ]
    for kw in header_keywords:
        if kw in lowered:
            return True
    return False


def _cell_text(cell) -> str:
    """Extract clean text from a selectolax node.

    Uses cell.text(deep=True) with Python str.strip() rather than the
    selectolax 0.4.x-only strip=True parameter, maintaining compatibility
    with the project's minimum selectolax>=0.3.21.
    """
    text = cell.text(deep=True).strip()
    return text or ""


def _parse_date(raw: str) -> date | None:
    """Parse a date string into a date object, or None on failure.

    Supports:
      - YYYY-MM-DD  (ISO 8601)
      - YYYY/MM/DD
      - DD/MM/YYYY  (common HK format)
      - YYYY        (year-only, date set to Jan 1)
    """
    if not raw:
        return None

    raw = raw.strip()

    # Try ISO 8601: YYYY-MM-DD
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Try YYYY/MM/DD
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date()
    except ValueError:
        pass

    # Try DD/MM/YYYY
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        pass

    # Try year-only
    try:
        return datetime.strptime(raw, "%Y").date()
    except ValueError:
        pass

    return None
