"""Wikipedia HK IPO list source.

Fetches Wikipedia pages listing initial public offerings on the Hong Kong
Stock Exchange by year. Uses the Wikipedia API to retrieve raw wikitext
(no scraping of rendered HTML), then parses wikitable markup into
ExternalIPO entries.

API endpoint:
    https://en.wikipedia.org/w/api.php?action=parse&page=...&prop=wikitext&format=json

Target pages by year (multiple candidates tried per year):
    - "List of initial public offerings on the Hong Kong Stock Exchange in <YEAR>"
    - "List of IPOs on the Hong Kong Stock Exchange in <YEAR>"

Each page is cached at data/validation/raw/wikipedia/<year>.json.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from hk_ipo.l1._http import ValidationHTTPClient, should_use_cache
from hk_ipo.l1.models import ExternalIPO

logger = logging.getLogger("hk_ipo")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_NAME = "wikipedia"

_WIKI_PAGE_CANDIDATES = [
    "List of initial public offerings on the Hong Kong Stock Exchange in {year}",
    "List of IPOs on the Hong Kong Stock Exchange in {year}",
]

_WIKI_API_URL = "https://en.wikipedia.org/w/api.php"


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

async def fetch_wikipedia(
    client: ValidationHTTPClient,
    start_year: int = 2010,
    end_year: int | None = None,
    *,
    force_refresh: bool = False,
) -> list[ExternalIPO]:
    """Fetch HK IPO lists from Wikipedia. Caches API responses.

    Tries multiple page-name candidates per year. On failure, logs to
    source_errors.json and returns [].

    Args:
        client: Configured ValidationHTTPClient.
        start_year: First year to fetch (default 2010).
        end_year: Last year to fetch (default: current calendar year).
        force_refresh: If True, bypass cache and re-fetch.

    Returns:
        list[ExternalIPO] with source="wikipedia".
    """
    if end_year is None:
        end_year = datetime.now().year

    if start_year > end_year:
        logger.warning(
            "start_year (%d) > end_year (%d) for wikipedia; returning []",
            start_year,
            end_year,
        )
        return []

    all_results: list[ExternalIPO] = []

    for year in range(start_year, end_year + 1):
        try:
            year_results = await _fetch_year(
                client, year, force_refresh=force_refresh,
            )
            all_results.extend(year_results)
        except Exception:
            logger.exception(
                "Failed to fetch wikipedia data for year %d; skipping.", year
            )
            _log_source_error(
                f"Exception while fetching wikipedia data for year {year}"
            )

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
    """Fetch and parse Wikipedia IPO data for a single year.

    Caches the full API JSON response under:
        data/validation/raw/wikipedia/<year>.json
    """
    cache_dir = _cache_dir() / "raw" / SOURCE_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{year}.json"

    # -- read from cache if fresh enough --------------------------------
    if should_use_cache(cache_path, force_refresh=force_refresh):
        logger.debug(
            "Using cached wikipedia data for year %d: %s", year, cache_path
        )
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Corrupt cache for year %d (%s); re-fetching.", year, e
            )
            # Fall through to network fetch
        else:
            wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
            title = data.get("parse", {}).get("title", "")
            source_url = _page_url_from_title(title)
            return _parse_wikitext(wikitext, source_url)

    # -- try each page-name candidate -----------------------------------
    for candidate in _WIKI_PAGE_CANDIDATES:
        page_title = candidate.format(year=year)
        source_url = _page_url_from_title(page_title)

        try:
            resp = await client.get(
                _WIKI_API_URL,
                params={
                    "action": "parse",
                    "page": page_title,
                    "prop": "wikitext",
                    "format": "json",
                },
            )
        except Exception:
            logger.debug(
                "HTTP request failed for wikipedia page '%s'; trying next candidate.",
                page_title,
            )
            continue

        if resp.status_code != 200:
            logger.debug(
                "Wikipedia API returned HTTP %d for page '%s'; trying next candidate.",
                resp.status_code,
                page_title,
            )
            continue

        try:
            data = resp.json()
        except Exception:
            logger.debug(
                "Failed to parse JSON for wikipedia page '%s'; trying next candidate.",
                page_title,
            )
            continue

        if "error" in data:
            logger.debug(
                "Wikipedia API error for page '%s': %s; trying next candidate.",
                page_title,
                data.get("error"),
            )
            continue

        if "parse" not in data or "wikitext" not in data.get("parse", {}):
            logger.debug(
                "Wikipedia API response missing parse/wikitext for page '%s'; "
                "trying next candidate.",
                page_title,
            )
            continue

        # Cache the successful response
        cache_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        wikitext = data["parse"]["wikitext"]["*"]
        return _parse_wikitext(wikitext, source_url)

    # All candidates failed
    logger.warning(
        "All page-name candidates failed for year %d; logging and returning [].",
        year,
    )
    _log_source_error(
        f"All page-name candidates failed for year {year}"
    )
    return []


def _page_url_from_title(title: str) -> str:
    """Build a readable Wikipedia page URL from a page title."""
    if not title:
        return "https://en.wikipedia.org/"
    # Replace spaces with underscores for the URL path
    safe_title = title.replace(" ", "_")
    return f"https://en.wikipedia.org/wiki/{safe_title}"


# ---------------------------------------------------------------------------
# Wikitext parser
# ---------------------------------------------------------------------------

def _parse_wikitext(wikitext: str, source_url: str) -> list[ExternalIPO]:
    """Parse MediaWiki table markup into ExternalIPO entries.

    Looks for {| class="wikitable" ... blocks. Extracts ticker (stock code),
    company name, and listing date columns from table rows.

    Args:
        wikitext: Raw wikitext from the Wikipedia API.
        source_url: Provenance URL for the data.

    Returns:
        list[ExternalIPO] (may be empty if no wikitable blocks found).
    """
    if not wikitext or not wikitext.strip():
        return []

    lines = wikitext.split("\n")
    results: list[ExternalIPO] = []

    in_wikitable = False
    current_cells: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect table start: {| class="wikitable..."
        if stripped.startswith("{|"):
            if 'class="wikitable' in stripped or "class='wikitable" in stripped:
                in_wikitable = True
                current_cells = []
            else:
                in_wikitable = False
            continue

        # Detect table end: |}
        if stripped == "|}":
            if in_wikitable:
                _flush_row(current_cells, source_url, results)
                current_cells = []
            in_wikitable = False
            continue

        if not in_wikitable:
            continue

        # Detect row separator: |-
        if stripped.startswith("|-"):
            _flush_row(current_cells, source_url, results)
            current_cells = []
            continue

        # Detect header row: ! prefix (skip)
        if stripped.startswith("!"):
            current_cells = []
            continue

        # Detect data cell: | prefix (single cell or || separated)
        if stripped.startswith("|"):
            cell_content = stripped[1:]  # Remove leading |
            # Split by || for inline multiple cells
            sub_cells = cell_content.split("||")
            for sc in sub_cells:
                sc = sc.strip()
                # Skip cells that are purely style attributes or empty
                if sc and not _is_style_attribute(sc):
                    current_cells.append(sc)

    # Handle any trailing row not terminated by |} or |-
    _flush_row(current_cells, source_url, results)

    return results


def _is_style_attribute(text: str) -> bool:
    """Return True if the text looks like a MediaWiki cell style attribute."""
    return bool(re.match(r'^[\w-]+\s*=\s*"[^"]*"$', text))


def _flush_row(
    cells: list[str], source_url: str, results: list[ExternalIPO],
) -> None:
    """Convert accumulated row cells into an ExternalIPO entry if valid."""
    if not cells:
        return
    ipo = _cells_to_ipo(cells, source_url)
    if ipo is not None:
        results.append(ipo)


def _cells_to_ipo(cells: list[str], source_url: str) -> ExternalIPO | None:
    """Interpret a list of cell strings as ticker, company name, and date.

    Heuristic approach:
      1. Scan cells for a numeric ticker (1-5 digits).
      2. Scan cells for a parseable date.
      3. Any remaining non-empty cell is treated as company name.

    Returns None if no ticker can be identified.
    """
    ticker: str | None = None
    company_name: str | None = None
    list_date: date | None = None

    for cell in cells:
        cell_stripped = cell.strip()
        if not cell_stripped:
            continue

        # Strip simple templates ({{...}}) from the cell before analysis
        clean = _strip_simple_templates(cell_stripped)

        # Try to find a ticker: digits only, 1-5 chars
        if ticker is None:
            ticker = _extract_ticker(clean)
            if ticker is not None:
                continue

        # Try to parse as a date
        if list_date is None:
            parsed = _parse_date(clean)
            if parsed is not None:
                list_date = parsed
                continue

        # Otherwise, it's a company name (use first non-ticker, non-date cell)
        if company_name is None:
            company_name = _clean_wikitext(cell_stripped)

    if ticker is None:
        return None

    return ExternalIPO(
        hk_ticker=ticker,
        company_name=company_name or "",
        list_date=list_date,
        source=SOURCE_NAME,
        source_url=source_url,
    )


def _extract_ticker(text: str) -> str | None:
    """Extract a ticker from cell text. Returns zero-padded 5-digit string or None.

    Strips templates {{Hs|...}} and other wrappers, then looks for a pure
    digit sequence of 1-5 characters.
    """
    if not text:
        return None
    # Remove any remaining templates, wiki links, HTML tags
    clean = _strip_simple_templates(text)
    clean = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", clean)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = re.sub(r"'''?", "", clean)
    clean = clean.strip()
    # Check if the result is pure digits
    if clean.isdigit() and len(clean) <= 5:
        return clean.zfill(5)
    # As a fallback, extract any digit sequence from the text
    digits = re.sub(r"\D", "", clean)
    if digits and len(digits) <= 5:
        return digits.zfill(5)
    return None


# ---------------------------------------------------------------------------
# Wikitext cleaning
# ---------------------------------------------------------------------------

def _strip_simple_templates(text: str) -> str:
    """Strip non-nested MediaWiki templates ({{...}}) from text.

    Handles simple templates like {{Hs|09988}}, {{flagicon|CHN}},
    {{dts|2019|11|26}}, etc. Does not handle nested templates — those
    are unlikely to appear in IPO table cells.
    """
    # Remove templates with balanced braces (no nesting)
    result = re.sub(r"\{\{[^{}]*\}\}", "", text)
    return result


def _clean_wikitext(text: str) -> str:
    """Clean common wikitext formatting from a cell value.

    Strips:
      - HTML comments <!-- ... -->
      - Wiki links [[...]] (keeps display text)
      - Bold/italic markers (''', '')
      - Templates {{...}}
      - <ref> tags
      - HTML tags
      - Extra whitespace
    """
    if not text:
        return ""
    # HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # <ref>...</ref> tags
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Templates
    text = _strip_simple_templates(text)
    # Wiki links [[Target|Display]] → Display, [[Target]] → Target
    text = re.sub(r"\[\[([^\]|]+?)\]\]", r"\1", text)
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    # Bold/italic
    text = re.sub(r"'''", "", text)
    text = re.sub(r"''", "", text)
    # HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = " ".join(text.split())
    return text.strip()


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> date | None:
    """Parse a date string into a date object, or None on failure.

    Supports:
      - YYYY-MM-DD  (ISO 8601)
      - YYYY/MM/DD
      - DD Month YYYY  (e.g. "6 May 2025", "26 November 2019")
      - DD Month YYYY with ordinals  (e.g. "20th September 2024")
      - DD/MM/YYYY  (common HK/UK format)
      - YYYY         (year-only fallback)
    """
    if not raw:
        return None

    raw = raw.strip()

    # Empty or obvious non-date
    if not raw:
        return None

    # Strip templates first (e.g. {{dts|2019|11|26}})
    raw = _strip_simple_templates(raw).strip()
    if not raw:
        return None

    # ISO 8601: YYYY-MM-DD
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass

    # YYYY/MM/DD
    try:
        return datetime.strptime(raw, "%Y/%m/%d").date()
    except ValueError:
        pass

    # DD/MM/YYYY
    try:
        return datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        pass

    # DD Month YYYY — try multiple variants
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    # Strip ordinal suffixes (1st, 2nd, 3rd, 4th, ...th) and retry
    ordinal_stripped = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
    if ordinal_stripped != raw:
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(ordinal_stripped, fmt).date()
            except ValueError:
                pass

    # Year-only fallback: YYYY
    try:
        return datetime.strptime(raw, "%Y").date()
    except ValueError:
        pass

    return None
