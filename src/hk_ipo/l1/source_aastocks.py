"""AAStocks listed IPO history source.

Fetches the AAStocks Listed IPO page and extracts individual IPO entries
(ticker, company name, listing date) from its server-rendered HTML table.

The page at http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx
renders a <table class="ns2 dataTable"> inside <div id="IPOListed"> with:
  - Column 0: arrow icon (ignored)
  - Column 1: <a>company name</a> + <a class="cls">TICKER.HK</a>
  - Column 2: listing date in YYYY/MM/DD format
  - Columns 3-12: financial data (ignored)

Pagination follows the pattern:
  listedipo.aspx?s=1&o=0&page=N

We walk pages until no rows are found or max_pages is reached (default 30).
Each page is cached at data/validation/raw/aastocks/page_<N>.html.
"""

from __future__ import annotations

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

SOURCE_NAME = "aastocks"
AASTOCKS_BASE_URL = (
    "http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx"
)
DEFAULT_MAX_PAGES = 30


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    """Return the root cache directory for validation data."""
    return Path("data/validation")


def _error_log_path() -> Path:
    """Return the path to source_errors.json."""
    return _cache_dir() / "source_errors.json"


def _log_source_error(reason: str) -> None:
    """Append a timestamped error entry to source_errors.json.

    Creates the file and parent directories if they do not exist.

    Note: this function performs a read-modify-write of the JSON file and is
    not concurrency-safe. In the current architecture, sources are fetched
    sequentially so concurrent writes are unlikely. Errors are rare and the
    file is capped at 50 entries, so the risk of data loss is negligible.
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

async def fetch_aastocks(
    client: ValidationHTTPClient,
    *,
    force_refresh: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[ExternalIPO]:
    """Scrape AAStocks listed IPO history. Caches raw responses.

    Walks paginated results until an empty page is encountered or max_pages
    is reached.

    Args:
        client: Configured ValidationHTTPClient.
        force_refresh: If True, bypass cache and re-fetch.
        max_pages: Safety limit for pagination walk (default 30).

    Returns:
        list[ExternalIPO] with source="aastocks".
        On failure, logs to source_errors.json and returns [].
    """
    all_results: list[ExternalIPO] = []

    for page_num in range(1, max_pages + 1):
        try:
            page_results = await _fetch_page(
                client,
                page_num,
                force_refresh=force_refresh,
            )
        except Exception:
            logger.exception(
                "Failed to fetch aastocks page %d; skipping.", page_num
            )
            _log_source_error(
                f"Exception while fetching aastocks page {page_num}"
            )
            continue

        if not page_results:
            # Empty page — reached the end of results.
            break

        all_results.extend(page_results)

    return all_results


# ---------------------------------------------------------------------------
# Per-page fetch
# ---------------------------------------------------------------------------

async def _fetch_page(
    client: ValidationHTTPClient,
    page_num: int,
    *,
    force_refresh: bool = False,
) -> list[ExternalIPO]:
    """Fetch and parse a single AAStocks IPO page.

    Caches the raw HTML under:
        data/validation/raw/aastocks/page_<N>.html
    """
    source_url = f"{AASTOCKS_BASE_URL}?s=1&o=0&page={page_num}"

    cache_dir = _cache_dir() / "raw" / SOURCE_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"page_{page_num}.html"

    # -- read from cache if fresh enough --------------------------------
    if should_use_cache(cache_path, force_refresh=force_refresh):
        logger.debug("Using cached aastocks page %d: %s", page_num, cache_path)
        html = cache_path.read_text(encoding="utf-8")
        return _parse_aastocks_page(html, source_url)

    # -- fetch from network ---------------------------------------------
    resp = await client.get(source_url)

    if resp.status_code != 200:
        logger.warning(
            "AAStocks page %d returned HTTP %d; skipping page.",
            page_num,
            resp.status_code,
        )
        raise RuntimeError(
            f"AAStocks page {page_num} returned HTTP {resp.status_code}"
        )

    # Cache the successful response
    cache_path.write_text(resp.text, encoding="utf-8")

    return _parse_aastocks_page(resp.text, source_url)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_aastocks_page(html: str, source_url: str) -> list[ExternalIPO]:
    """Parse one page of AAStocks IPO table into ExternalIPO entries.

    The AAStocks table uses this structure:
        <div id="IPOListed">
          <table class="ns2 dataTable">
            <thead>...</thead>
            <tbody>
              <tr>
                <!-- col 0: arrow icon -->
                <td></td>
                <!-- col 1: company name (1st <a>) + code (2nd <a class="cls">) -->
                <td class="txt_l">
                  <a href="...">COMPANY NAME</a><br/>
                  <a class="cls" href="...">01234.HK</a>
                </td>
                <!-- col 2: listing date -->
                <td class="txt_r cls">YYYY/MM/DD</td>
                <!-- cols 3-12: financial data (ignored) -->
                ...
              </tr>
            </tbody>
          </table>
        </div>

    Args:
        html: The raw HTML content of one AAStocks IPO page.
        source_url: Provenance URL for this page.

    Returns:
        list[ExternalIPO] (may be empty if no data rows found).
    """
    if not html or not html.strip():
        return []

    tree = HTMLParser(html)

    # Find the IPO table: <div id="IPOListed"> > <table class="ns2 dataTable">
    table = tree.css_first("table.ns2.dataTable")
    if table is None:
        return []

    # Get all rows from tbody
    rows = table.css("tbody tr")
    if not rows:
        rows = table.css("tr")

    results: list[ExternalIPO] = []

    for row in rows:
        cells = row.css("td")
        if len(cells) < 3:
            continue

        # Skip the "no data" row: single cell with colspan
        if len(cells) == 1:
            continue

        # Column 1 (index 1): company name + stock code
        name_cell = cells[1]
        # Company name is the first <a> tag
        name_links = name_cell.css("a")
        if not name_links:
            # No links at all — skip this row
            continue

        # The stock code link has class "cls"
        code_link = name_cell.css_first("a.cls")
        if code_link is None:
            # Some rows may have the code link without class="cls"
            # Try the last <a> as fallback
            if len(name_links) >= 2:
                code_link = name_links[-1]
            else:
                continue

        ticker_raw = code_link.text(deep=True).strip()
        if not ticker_raw:
            continue

        # Strip trailing ".HK" from ticker like "02259.HK"
        ticker = ticker_raw.replace(".HK", "").replace(".hk", "").strip()
        if not ticker or not ticker.isdigit():
            continue

        # Company name: search for a link that is NOT the stock-code link.
        # The real page has <a>Company</a><br/><a class="cls">TICKER.HK</a>.
        # If only the "cls" link exists (no company name), default to "".
        company_name = ""
        for link in name_links:
            link_classes = (link.attributes.get("class") or "").split()
            if "cls" not in link_classes:
                company_name = link.text(deep=True).strip()
                break

        # Column 2 (index 2): listing date
        date_cell = cells[2]
        date_raw = date_cell.text(deep=True).strip()
        list_date = _parse_date(date_raw)

        results.append(
            ExternalIPO(
                hk_ticker=ticker,
                company_name=company_name,
                list_date=list_date,
                source=SOURCE_NAME,
                source_url=source_url,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> date | None:
    """Parse an AAStocks date string into a date object, or None on failure.

    AAStocks uses YYYY/MM/DD format (e.g. "2025/09/30").
    Also handles:
      - YYYY-MM-DD (ISO 8601)
      - N/A, empty, or any unparsable value → None
      - YYYY (year-only, date set to Jan 1) as fallback
    """
    if not raw:
        return None

    raw = raw.strip()

    if not raw or raw.upper() == "N/A":
        return None

    # Primary format: YYYY/MM/DD
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date()
    except ValueError:
        pass

    # ISO 8601: YYYY-MM-DD
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass

    # DD/MM/YYYY (only when unambiguous: first segment > 12 means it
    # cannot be a month, so it must be a day value in DD/MM/YYYY format)
    parts = raw.split("/")
    if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 12:
        try:
            return datetime.strptime(raw, "%d/%m/%Y").date()
        except ValueError:
            pass

    # Year-only fallback
    try:
        return datetime.strptime(raw, "%Y").date()
    except ValueError:
        pass

    return None
