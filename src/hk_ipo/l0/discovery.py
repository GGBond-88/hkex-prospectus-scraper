"""HKEX prospectus discovery: JSON API primary, HTML title-search fallback."""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, timezone
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

logger = logging.getLogger("hk_ipo")

# HKEX category codes for "final Prospectus / GEM Listing Document"
T1_LISTING_DOCUMENTS = "40000"
T2_PROSPECTUS = "40100"


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


def _parse_retry_after_disc(response: httpx.Response) -> float | None:
    """Parse Retry-After header from a 429 response. Returns seconds or None."""
    retry_after = (response.headers.get("Retry-After", "") or "").strip()
    if not retry_after:
        return None
    # Integer seconds.
    try:
        return float(int(retry_after))
    except (ValueError, TypeError):
        pass
    # HTTP-date.
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(retry_after)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


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
        user_agent: str = "hk-ipo-research/0.1 (research)",
        page_size: int = 100,
        inter_window_sleep: float = 0.0,  # 0 in tests; real run uses 1.5
        per_request_max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.json_api_base = json_api_base
        self.html_search_base = html_search_base
        self.pdf_base_url = pdf_base_url
        self.log_dir = log_dir
        self.user_agent = user_agent
        self.page_size = page_size
        self.inter_window_sleep = inter_window_sleep
        self.per_request_max_attempts = per_request_max_attempts
        self._client = client
        self._owns_client = client is None
        self.failed_windows: list[dict[str, str]] = []

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
        try:
            async for f in self._fetch_json_window(w_start, w_end):
                yield f
            return
        except Exception as json_err:
            logger.warning(
                "JSON path failed for %s..%s: %s; falling back to HTML",
                w_start, w_end, json_err,
            )
        async for f in self._fetch_html_window(w_start, w_end):
            yield f

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
