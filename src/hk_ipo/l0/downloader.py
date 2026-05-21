"""HTTP download of HKEX PDFs: streaming, hashed, atomic, retried."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hk_ipo.l0.models import DownloadOutcome, DownloadResult, Filing

logger = logging.getLogger("hk_ipo")


def sweep_orphan_tmp_files(raw_pdfs_dir: Path) -> int:
    """Delete leftover .pdf.tmp files from a crashed prior run. Returns count removed."""
    if not raw_pdfs_dir.exists():
        return 0
    count = 0
    for p in raw_pdfs_dir.glob("*.pdf.tmp"):
        try:
            p.unlink()
            count += 1
        except OSError:
            logger.warning("could not remove orphan tmp file: %s", p)
    return count


@dataclass(slots=True)
class _RetryableHTTPError(Exception):
    response: httpx.Response

    def __str__(self) -> str:
        return f"HTTP {self.response.status_code}"


class _TerminalHTTPError(Exception):
    pass


class _HashMismatchError(Exception):
    pass


class PDFDownloader:
    def __init__(
        self,
        raw_pdfs_dir: Path,
        *,
        user_agent: str = "hk-ipo-research/0.1 (research)",
        max_workers: int = 4,
        per_request_max_attempts: int = 5,
        jitter_seconds: tuple[float, float] = (0.3, 0.8),  # spec section 7 default
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.raw_pdfs_dir = raw_pdfs_dir
        self.user_agent = user_agent
        self.max_workers = max_workers
        self.per_request_max_attempts = per_request_max_attempts
        self.jitter_seconds = jitter_seconds
        self._client = client
        self._owns_client = client is None
        self._sem = asyncio.Semaphore(max_workers)

    async def __aenter__(self) -> "PDFDownloader":
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

    async def download(self, filing: Filing) -> DownloadResult:
        """Download one filing. Returns a DownloadResult (never raises for HTTP errors)."""
        own = False
        if self._client is None:
            await self.__aenter__()
            own = True
        try:
            return await self._download_one(filing)
        finally:
            if own:
                await self.__aexit__()

    async def download_many(self, filings: Iterable[Filing]) -> list[DownloadResult]:
        filings = list(filings)
        if not filings:
            return []
        async with self:
            tasks = [asyncio.create_task(self._download_one(f)) for f in filings]
            return await asyncio.gather(*tasks)

    async def _download_one(self, filing: Filing) -> DownloadResult:
        self.raw_pdfs_dir.mkdir(parents=True, exist_ok=True)
        target = self.raw_pdfs_dir / f"{filing.hk_ticker}.pdf"
        tmp = self.raw_pdfs_dir / f"{filing.hk_ticker}.pdf.tmp"

        attempts = 0
        async with self._sem:
            await self._jitter()
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self.per_request_max_attempts),
                    wait=wait_exponential(multiplier=1, min=1, max=16),
                    retry=retry_if_exception_type((_RetryableHTTPError, httpx.TransportError)),
                    reraise=True,
                ):
                    with attempt:
                        attempts += 1
                        sha, size = await self._stream_to_tmp(filing.doc_url, tmp)
                os.replace(tmp, target)
                return DownloadResult(
                    hk_ticker=filing.hk_ticker,
                    outcome=DownloadOutcome.SUCCESS,
                    file_path=target.name,
                    file_sha256=sha,
                    file_size_bytes=size,
                    attempts=attempts,
                )
            except _TerminalHTTPError as e:
                _safe_unlink(tmp)
                return DownloadResult(
                    hk_ticker=filing.hk_ticker,
                    outcome=DownloadOutcome.FAILED,
                    attempts=attempts,
                    error=str(e),
                )
            except (_RetryableHTTPError, httpx.TransportError, _HashMismatchError) as e:
                _safe_unlink(tmp)
                return DownloadResult(
                    hk_ticker=filing.hk_ticker,
                    outcome=DownloadOutcome.FAILED,
                    attempts=attempts,
                    error=f"{type(e).__name__}: {e} after {attempts} attempts",
                )

    async def _stream_to_tmp(self, url: str, tmp: Path) -> tuple[str, int]:
        """Stream PDF to tmp file.

        429 -> internal retry loop (3 attempts, Retry-After or 30s/60s/120s).
        5xx -> _RetryableHTTPError (outer tenacity loop handles retry).
        4xx (non-429) -> _TerminalHTTPError.
        """
        assert self._client is not None
        rate_limit_attempt = 0
        while True:
            rate_limit_attempt += 1
            h = hashlib.sha256()
            size = 0
            async with self._client.stream("GET", url) as resp:
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise _TerminalHTTPError(f"HTTP {resp.status_code} for {url}")
                if resp.status_code >= 500:
                    raise _RetryableHTTPError(resp)
                if resp.status_code == 429:
                    if rate_limit_attempt >= 3:
                        raise _TerminalHTTPError(
                            f"HTTP 429 for {url} after 3 attempts"
                        )
                    delay = _parse_retry_after(resp)
                    if delay is None:
                        delay = [30.0, 60.0, 120.0][rate_limit_attempt - 1]
                    await asyncio.sleep(delay)
                    continue  # restart stream GET
                # 2xx -- stream body, hash, return.
                with tmp.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        fh.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                return h.hexdigest(), size

    async def _jitter(self) -> None:
        lo, hi = self.jitter_seconds
        if hi <= 0:
            return
        await asyncio.sleep(random.uniform(lo, hi))


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from a 429 response.

    Returns seconds to wait (float), or None if absent/unparseable.
    Handles both integer-seconds and HTTP-date per RFC 7231.
    """
    retry_after = (response.headers.get("Retry-After", "") or "").strip()
    if not retry_after:
        return None
    # Try integer seconds.
    try:
        return float(int(retry_after))
    except (ValueError, TypeError):
        pass
    # Try HTTP-date (e.g. "Wed, 21 Oct 2015 07:28:00 GMT").
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(retry_after)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None
