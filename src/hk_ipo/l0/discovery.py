"""HKEX prospectus discovery.

MVP-1 (EI-001 resolution, 2026-05-22) — discovery now uses the live HKEX
search flow reverse-engineered from browser DevTools:

  1. (optional) `partial.do`  — JSONP autocomplete that maps a ticker code
     to HKEX's internal numeric `stockId`. Required only for ticker-scoped
     queries (refresh / golden fixtures).
  2. POST `titlesearch.xhtml`  — primary search endpoint. Returns the full
     results table as server-rendered HTML. Parsed with selectolax.

The legacy `_fetch_json_window` / `_parse_filing_from_json` helpers are kept
in the file for backward-compatibility with existing mock-based tests but
are no longer called from the primary `list_filings` flow.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from selectolax.parser import HTMLParser
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hk_ipo.l0.models import Filing, pad_ticker
from hk_ipo.l1._http import _parse_retry_after_disc

logger = logging.getLogger("hk_ipo")

# Legacy category codes (kept for backward-compat tests; no longer used by
# the live discovery flow — the new endpoint returns HTML keyed by headline).
T1_LISTING_DOCUMENTS = "40000"
T2_PROSPECTUS = "40100"

# Headline prefixes used by the new HTML response. Discovery yields all rows
# matching these prefixes; filter.py + orchestrator handle the final
# language / finality / dedup decision.
_HEADLINE_LISTING_DOCS = "Listing Documents - "
_HEADLINE_APP_PROOFS = "Application Proofs"

# IPO-relevant tier-2 sub-types under "Listing Documents". Matches the bracketed
# suffix of the headline, e.g. "Listing Documents - [Offer for Subscription]".
# Everything else (ETF / CIS, Rights Issue, Capitalisation Issue, etc.) is
# Listing-Documents-shaped but NOT an IPO prospectus and is_final=False below.
_IPO_SUBTYPES = (
    "Offer for Subscription",
    "Introduction",
    "Offer for Sale",
    "Placing of Securities of a Class New to Listing",
)

# HKEX tier-2 category codes for IPO subtypes (under t1code=30000 "Listing
# Documents"). Posting one per subtype eliminates ETF/CIS/Rights-Issue noise at
# the server boundary, since t2code=-2 floods the response with ~90% non-IPO rows
# that push real IPOs past the 100-row response limit (Bug #1 / spec_revise_new).
_IPO_T2CODES = {
    "30500": "Introduction",
    "30600": "Offer for Sale",
    "30700": "Offer for Subscription",
    "31000": "Placing of Securities of a Class New to Listing",
}

# Title patterns that mark a row as NOT the canonical prospectus PDF, even
# when its headline says "Listing Documents - [Offer for Subscription]".
_NON_PROSPECTUS_TITLE_MARKERS = (
    "APPLICATION FORM",   # White / Yellow / Green forms
    "FORMAL NOTICE",      # Newspaper ad, ~500KB stub
)


def iter_monthly_windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Yield (window_start, window_end) date pairs, one per calendar month, inclusive."""
    if end < start:
        raise ValueError(f"end {end} before start {start}")
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        yield cur, date(cur.year, cur.month, last_day)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


HKEX_BASE = "https://www1.hkexnews.hk"


def _absolute(url: str, pdf_base: str = HKEX_BASE) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return pdf_base.rstrip("/") + url
    return f"{pdf_base.rstrip('/')}/{url}"


def _market_code(raw: str) -> str:
    raw = (raw or "").upper()
    if raw in ("SEHK", "MAIN", "MB"):
        return "MB"
    if raw == "GEM":
        return "GEM"
    raise ValueError(f"unknown market {raw!r}")


def _language(raw: str | None, url: str) -> str:
    raw = (raw or "").upper()
    if raw == "E":
        return "en"
    if raw == "C":
        return "zh"
    if raw == "B":
        return "bilingual"
    # Fallback: URL suffix.
    lower = (url or "").lower()
    if lower.endswith("_e.pdf"):
        return "en"
    if lower.endswith("_c.pdf"):
        return "zh"
    return "en"  # last-resort default; filter will catch via title


def _parse_publish_date(s: str) -> datetime:
    # HKEX format: "YYYY-MM-DD HH:MM:SS" in Hong Kong local time (UTC+8).
    naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    from datetime import timedelta

    hkt = timezone(timedelta(hours=8))
    return naive.replace(tzinfo=hkt).astimezone(timezone.utc)


def _parse_filing_from_json(hit: dict[str, Any], pdf_base: str = HKEX_BASE) -> Filing | None:
    try:
        t1 = str(hit.get("T1_CODE", ""))
        if t1 != T1_LISTING_DOCUMENTS:
            return None
        market = _market_code(str(hit.get("MARKET", "")))
        doc_type = "Listing Document - GEM" if market == "GEM" else "Prospectus"
        url = _absolute(str(hit.get("FILE_LINK", "")), pdf_base)
        title = str(hit.get("TITLE", ""))
        # is_final: spec excludes A1 / PHIP / supplemental, which HKEX typically marks via title prefixes.
        title_upper = title.upper()
        is_final = not any(
            tag in title_upper
            for tag in ("APPLICATION PROOF", "POST HEARING INFORMATION", "PHIP", "SUPPLEMENT")
        )
        return Filing(
            hk_ticker=pad_ticker(str(hit.get("STOCK_CODE", ""))),
            doc_id=str(hit.get("DOC_ID", "")),
            doc_title=title or "Listing Document",
            doc_url=url,
            doc_type=doc_type,
            market=market,
            language=_language(hit.get("LANGUAGE_CD"), url),
            is_final=is_final,
            publish_date=_parse_publish_date(str(hit.get("DATE_TIME"))),
            company_name_en=hit.get("STOCK_NAME_EN"),
            company_name_zh=hit.get("STOCK_NAME_C"),
        )
    except (KeyError, ValueError) as e:
        logger.warning("dropping malformed hit %r: %s", hit, e)
        return None


class HKEXDiscoveryClient:
    def __init__(
        self,
        *,
        json_api_base: str,
        html_search_base: str,
        pdf_base_url: str = HKEX_BASE,
        log_dir: Path,
        partial_lookup_base: str = "https://www1.hkexnews.hk/search/partial.do",
        user_agent: str = "hk-ipo-research/0.1 (research)",
        page_size: int = 100,
        inter_window_sleep: float = 0.0,  # 0 in tests; real run uses 1.5
        per_request_max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.json_api_base = json_api_base
        self.html_search_base = html_search_base
        self.partial_lookup_base = partial_lookup_base
        self.pdf_base_url = pdf_base_url
        self.log_dir = log_dir
        self.user_agent = user_agent
        self.page_size = page_size
        self.inter_window_sleep = inter_window_sleep
        self.per_request_max_attempts = per_request_max_attempts
        self._client = client
        self._owns_client = client is None
        self.failed_windows: list[dict[str, str]] = []
        self._stock_id_cache: dict[str, int | None] = {}

    async def __aenter__(self) -> "HKEXDiscoveryClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self.user_agent},
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        # Persist failed-window log even on success.
        if self.failed_windows:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            (self.log_dir / "failed_windows.json").write_text(
                json.dumps(self.failed_windows, indent=2), encoding="utf-8",
            )

    async def list_filings(
        self, start: date, end: date,
    ) -> AsyncIterator[Filing]:
        own = self._client is None
        if own:
            await self.__aenter__()
        try:
            windows = list(iter_monthly_windows(start, end))
            for i, (w_start, w_end) in enumerate(windows):
                try:
                    async for filing in self._fetch_window(w_start, w_end):
                        yield filing
                except Exception as e:  # one bad window must not halt the job
                    logger.exception("window %s failed permanently: %s", w_start, e)
                    self.failed_windows.append({
                        "start": w_start.isoformat(),
                        "end": w_end.isoformat(),
                        "error": str(e),
                    })
                if i < len(windows) - 1:
                    await asyncio.sleep(self.inter_window_sleep)
        finally:
            if own:
                await self.__aexit__()

    async def _fetch_window(
        self, w_start: date, w_end: date,
    ) -> AsyncIterator[Filing]:
        """MVP-1: primary path is POST titlesearch.xhtml (HTML response).

        Legacy JSON path is no longer attempted live (EI-001: the JSON
        endpoint was retired). It remains in the file for backward-compat
        with unit tests that mock the old endpoint shape.
        """
        async for f in self._post_search_window(w_start, w_end, stock_id=None):
            yield f

    async def lookup_stock_id(self, ticker: str) -> int | None:
        """Map a 5-digit ticker to HKEX's internal numeric stockId.

        Hits the JSONP autocomplete endpoint `partial.do`. Returns None if no
        exact match is found. Cached in-memory per client lifetime.
        """
        normalised = pad_ticker(ticker)
        if normalised in self._stock_id_cache:
            return self._stock_id_cache[normalised]

        assert self._client is not None
        # HKEX's autocomplete accepts the ticker with or without leading zeros;
        # we send it unpadded to match what a human types in the search box.
        query_name = normalised.lstrip("0") or "0"
        params = {
            "callback": "callback",
            "lang": "EN",
            "type": "A",
            "name": query_name,
            "market": "SEHK",
            "_": str(int(time.time() * 1000)),
        }
        resp = await self._get_with_retry(self.partial_lookup_base, params=params)
        resp.raise_for_status()
        # Strip the JSONP wrapper: `callback(...);` or `callback(...)`.
        body = resp.text.strip()
        m = re.match(r"^[A-Za-z_$][\w$]*\((.*)\)\s*;?\s*$", body, re.DOTALL)
        payload = m.group(1) if m else body
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.warning("partial.do returned unparseable body for %s: %s",
                           ticker, e)
            self._stock_id_cache[normalised] = None
            return None

        for item in data or []:
            code = str(item.get("code", "")).strip()
            if pad_ticker(code) == normalised:
                stock_id = int(item.get("stockId"))
                self._stock_id_cache[normalised] = stock_id
                return stock_id
        self._stock_id_cache[normalised] = None
        return None

    async def _post_search_window(
        self, w_start: date, w_end: date, *, stock_id: int | None,
        _seen: set[tuple[str, str]] | None = None,
        _depth: int = 0,
    ) -> AsyncIterator[Filing]:
        """POST titlesearch.xhtml once per IPO t2code subtype, dedupe results.

        Bug #1 fix: instead of one POST with t2code=-2 (which floods every
        query with ETF/CIS noise that can push real IPO rows past the 100-row
        response limit), we POST once per IPO subtype (30500/30600/30700/31000).
        Each per-subtype response is small (well under 100 rows in any month)
        and contains only IPO material — no ETF, Rights Issue, or
        Capitalisation Issue rows.

        Bug #2 mitigation: if any subtype returns >= page_size rows, the
        window is split in half and each half is queried recursively (up to
        depth 3). This avoids needing full JSF AJAX pagination (ViewState /
        loadMore). Per-subtype queries rarely trigger this — only 5-10% of
        monthly windows have >100 Offer-for-Subscription rows.

        Results are deduplicated by (hk_ticker, doc_url) because the same
        filing can occasionally appear under two subtypes (e.g. a hybrid
        "Capitalisation Issue / Offer for Subscription" headline would match
        in both a broad -2 query and a targeted 30700 query, though that
        scenario is now moot since we never POST -2).
        """
        assert self._client is not None
        if _seen is None:
            _seen = set()
        max_depth = 5
        min_window_days = 2  # don't split windows smaller than 2 days

        url = self.html_search_base
        if "?" not in url:
            url = f"{url}?lang=en"

        max_total = 0
        for t2code, _label in sorted(_IPO_T2CODES.items()):
            form_data = {
                "lang": "EN",
                "category": "0",
                "market": "SEHK",
                "searchType": "1",
                "documentType": "-1",
                "t1code": "30000",
                "t2Gcode": t2code,
                "t2code": t2code,
                "stockId": str(stock_id) if stock_id else "",
                "from": w_start.strftime("%Y%m%d"),
                "to": w_end.strftime("%Y%m%d"),
                "title": "",
            }
            resp = await self._post_with_retry(url, data=form_data)
            resp.raise_for_status()
            self._archive_html_response(w_start, resp.text, suffix=f"t2_{t2code}")
            total = _parse_total_records(resp.text)
            if total > max_total:
                max_total = total
            for f in _parse_filings_from_search_html(resp.text, pdf_base=self.pdf_base_url):
                key = (f.hk_ticker, f.doc_url)
                if key not in _seen:
                    _seen.add(key)
                    yield f

        # If any subtype had more rows than the page can hold, and the window
        # is still large enough to split, recurse into two halves.
        window_days = (w_end - w_start).days
        if max_total > self.page_size and window_days >= min_window_days * 2 and _depth < max_depth:
            mid = w_start + (w_end - w_start) // 2
            logger.debug("splitting window %s..%s (total=%d > page=%d, depth=%d)",
                         w_start, w_end, max_total, self.page_size, _depth)
            async for f in self._post_search_window(
                w_start, mid, stock_id=stock_id, _seen=_seen, _depth=_depth + 1,
            ):
                yield f
            async for f in self._post_search_window(
                mid + timedelta(days=1), w_end, stock_id=stock_id,
                _seen=_seen, _depth=_depth + 1,
            ):
                yield f

    async def _post_with_retry(
        self, url: str, *, data: dict[str, str],
    ) -> httpx.Response:
        """POST with retry: same envelope as _get_with_retry (5xx + 429)."""
        assert self._client is not None
        rate_limit_attempts = 0
        while True:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.per_request_max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
                reraise=True,
            ):
                with attempt:
                    r = await self._client.post(url, data=data)
                    if r.status_code == 429:
                        rate_limit_attempts += 1
                        if rate_limit_attempts > 3:
                            raise RuntimeError(
                                f"HTTP 429 for {url} after 3 rate-limit retries"
                            )
                        delay = _parse_retry_after_disc(r)
                        if delay is None:
                            delay = [30.0, 60.0, 120.0][rate_limit_attempts - 1]
                        await asyncio.sleep(delay)
                        break
                    if r.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}",
                            request=r.request, response=r,
                        )
                    return r

    def _archive_html_response(self, w_start: date, html: str, suffix: str = "") -> None:
        archive_dir = self.log_dir / "discovery"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stem = w_start.strftime("%Y-%m-%d")
        if suffix:
            stem = f"{stem}-{suffix}"
        path = archive_dir / f"{stem}-html.html"
        path.write_text(html, encoding="utf-8")

    async def _fetch_json_window(
        self, w_start: date, w_end: date,
    ) -> AsyncIterator[Filing]:
        assert self._client is not None
        page = 1
        while True:
            params = {
                "t1code": T1_LISTING_DOCUMENTS,
                "t2code": T2_PROSPECTUS,
                "from": w_start.strftime("%Y%m%d"),
                "to": w_end.strftime("%Y%m%d"),
                "page": str(page),
                "pageSize": str(self.page_size),
            }
            resp = await self._get_with_retry(self.json_api_base, params=params)
            resp.raise_for_status()
            data = resp.json()
            self._archive_response(w_start, page, data)
            hits = data.get("hits") or []
            for hit in hits:
                f = _parse_filing_from_json(hit, pdf_base=self.pdf_base_url)
                if f is not None:
                    yield f
            total = int(data.get("total", 0))
            if page * self.page_size >= total:
                return
            page += 1

    async def _fetch_html_window(
        self, w_start: date, w_end: date,
    ) -> AsyncIterator[Filing]:
        assert self._client is not None
        params = {
            "t1code": T1_LISTING_DOCUMENTS,
            "t2code": T2_PROSPECTUS,
            "from": w_start.strftime("%Y%m%d"),
            "to": w_end.strftime("%Y%m%d"),
        }
        resp = await self._get_with_retry(self.html_search_base, params=params)
        resp.raise_for_status()
        for f in _parse_filings_from_html(resp.text, pdf_base=self.pdf_base_url):
            yield f

    async def _get_with_retry(
        self, url: str, *, params: dict[str, str],
    ) -> httpx.Response:
        """GET with retry.

        5xx -> tenacity exponential backoff via HTTPStatusError.
        429 -> internal 3-attempt loop with Retry-After fallback (spec section 7).
        """
        assert self._client is not None
        rate_limit_attempts = 0
        while True:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.per_request_max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
                reraise=True,
            ):
                with attempt:
                    r = await self._client.get(url, params=params)
                    if r.status_code == 429:
                        rate_limit_attempts += 1
                        if rate_limit_attempts > 3:
                            raise RuntimeError(
                                f"HTTP 429 for {url} after 3 rate-limit retries"
                            )
                        delay = _parse_retry_after_disc(r)
                        if delay is None:
                            delay = [30.0, 60.0, 120.0][rate_limit_attempts - 1]
                        await asyncio.sleep(delay)
                        break  # exit inner AsyncRetrying, restart with fresh tenacity
                    if r.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}",
                            request=r.request, response=r,
                        )
                    return r
            # Inner loop exited normally (should not happen with reraise=True);
            # the outer while True continues.

    def _archive_response(self, w_start: date, page: int, data: dict[str, Any]) -> None:
        archive_dir = self.log_dir / "discovery"
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f"{w_start.strftime('%Y-%m')}-page{page}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _strip_label(text: str, label: str) -> str:
    """Strip an optional 'Label: ' prefix (mobile-list-heading style) from a cell value."""
    if label in text:
        text = text.split(label, 1)[1]
    return text.strip()


def _parse_dmy_datetime(s: str) -> datetime:
    """Parse HKEX HTML date format: 'DD/MM/YYYY HH:MM'. HKT -> UTC."""
    naive = datetime.strptime(s, "%d/%m/%Y %H:%M")
    hkt = timezone(timedelta(hours=8))
    return naive.replace(tzinfo=hkt).astimezone(timezone.utc)


_TOTAL_RECORDS_RE = re.compile(r"Total records found:\s*(\d+)", re.IGNORECASE)


def _parse_total_records(html: str) -> int:
    """Extract the 'Total records found: N' count from a titlesearch HTML page."""
    m = _TOTAL_RECORDS_RE.search(html)
    return int(m.group(1)) if m else 0


_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _detect_language_from_title(title: str) -> str:
    has_cjk = bool(_CJK_RE.search(title))
    has_latin = bool(_LATIN_RE.search(title))
    if has_cjk and has_latin:
        return "bilingual"
    if has_cjk:
        return "zh"
    return "en"


def _market_from_ticker(ticker: str) -> str:
    """Best-effort market inference. GEM tickers are 5-digit codes starting with '8'."""
    return "GEM" if ticker.startswith("8") else "MB"


def _parse_filings_from_search_html(
    html: str, pdf_base: str = HKEX_BASE,
) -> Iterator[Filing]:
    """Parse the HTML response from POST titlesearch.xhtml.

    Yields a Filing for each result-table row whose headline indicates IPO
    listing material. Non-IPO rows (announcements, NDDRs, constitutional,
    etc.) are dropped silently at this boundary — they are not part of L0's
    output domain.

    Within IPO rows:
      - `headline.startswith("Listing Documents - ")` → is_final=True candidate
        (refined by title check: application forms are flagged is_final=False
        so filter.py will skip them as wrong_doc_type).
      - `headline.startswith("Application Proofs")` → is_final=False (A1/PHIP).
      - `.htm` href → dropped (multi-file HTML index pages).
    """
    tree = HTMLParser(html)
    for row in tree.css("table tbody tr"):
        try:
            f = _parse_search_row(row, pdf_base=pdf_base)
            if f is not None:
                yield f
        except (AttributeError, ValueError) as e:
            logger.warning("HTML search row skipped: %s", e)


def _parse_search_row(row: Any, pdf_base: str) -> Filing | None:
    # Release time
    rt_cell = row.css_first("td.release-time")
    if rt_cell is None:
        return None
    rt_text = _strip_label(rt_cell.text(strip=True), "Release Time:")
    publish_date = _parse_dmy_datetime(rt_text)

    # Stock code
    code_cell = row.css_first("td.stock-short-code")
    if code_cell is None:
        return None
    code_text = _strip_label(code_cell.text(strip=True), "Stock Code:")
    if not code_text or not code_text.isdigit():
        return None
    hk_ticker = pad_ticker(code_text)

    # Stock name (English short name; HTML response gives only one)
    name_cell = row.css_first("td.stock-short-name")
    company_name_en = (
        _strip_label(name_cell.text(strip=True), "Stock Short Name:")
        if name_cell else None
    )

    # Headline (document category)
    headline_div = row.css_first("div.headline")
    headline = headline_div.text(strip=True) if headline_div else ""

    # Drop rows that are not IPO material at all (the bulk: A&N, NDDRs, etc.)
    is_listing_doc = headline.startswith(_HEADLINE_LISTING_DOCS)
    is_app_proof = headline.startswith(_HEADLINE_APP_PROOFS)
    if not (is_listing_doc or is_app_proof):
        return None

    # Document link
    link = row.css_first("div.doc-link a")
    if link is None:
        return None
    href = link.attributes.get("href", "") or ""
    if not href:
        return None
    title = link.text(strip=True)
    doc_url = _absolute(href, pdf_base)

    # Drop non-PDF rows (HTM multi-file index pages — not the prospectus itself)
    if not href.lower().endswith(".pdf"):
        return None

    # is_final: only Listing-Documents-headline rows are "final" candidates,
    # AND the tier-2 subtype must be an IPO type (Offer for Subscription,
    # Introduction, etc.), AND the title must not be an application form
    # or formal notice (those live under the same headline but are not
    # the prospectus itself).
    title_upper = title.upper()
    title_is_non_prospectus = any(
        m in title_upper for m in _NON_PROSPECTUS_TITLE_MARKERS
    )
    is_ipo_subtype = any(f"[{sub}]" in headline for sub in _IPO_SUBTYPES)
    is_final = is_listing_doc and is_ipo_subtype and not title_is_non_prospectus

    market = _market_from_ticker(hk_ticker)
    doc_type = "Listing Document - GEM" if market == "GEM" else "Prospectus"
    language = _detect_language_from_title(title)

    return Filing(
        hk_ticker=hk_ticker,
        doc_id=Path(doc_url).stem,
        doc_title=title,
        doc_url=doc_url,
        doc_type=doc_type,
        market=market,
        language=language,
        is_final=is_final,
        publish_date=publish_date,
        company_name_en=company_name_en,
    )


def _parse_filings_from_html(html: str, pdf_base: str = HKEX_BASE) -> list[Filing]:
    tree = HTMLParser(html)
    out: list[Filing] = []
    for row in tree.css("tr.row"):
        try:
            date_str = row.css_first("td.release-date").text(strip=True)
            stock_code = row.css_first("td.stock-code").text(strip=True)
            stock_name = row.css_first("td.stock-name").text(strip=True)
            link = row.css_first("td.title a")
            doc_url = _absolute(link.attributes.get("href", ""), pdf_base)
            title = link.text(strip=True)
            market_text = row.css_first("td.market").text(strip=True)
            market = _market_code(market_text)
            doc_type = "Listing Document - GEM" if market == "GEM" else "Prospectus"
            language = _language(None, doc_url)
            out.append(Filing(
                hk_ticker=pad_ticker(stock_code),
                doc_id=Path(doc_url).stem,
                doc_title=title,
                doc_url=doc_url,
                doc_type=doc_type,
                market=market,
                language=language,
                is_final="APPLICATION PROOF" not in title.upper()
                         and "PHIP" not in title.upper()
                         and "SUPPLEMENT" not in title.upper(),
                publish_date=_parse_publish_date(date_str),
                company_name_en=stock_name,
            ))
        except (AttributeError, ValueError) as e:
            logger.warning("HTML row skipped: %s", e)
    return out
