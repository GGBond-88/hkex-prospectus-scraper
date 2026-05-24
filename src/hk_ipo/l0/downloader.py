"""HTTP download of HKEX PDFs: streaming, hashed, atomic, retried."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hk_ipo.l0.models import DownloadOutcome, DownloadResult, Filing
from hk_ipo.l1._http import _parse_retry_after_disc

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
                try:
                    _safe_replace(tmp, target, max_retries=10, base_delay=0.2)
                except (PermissionError, OSError) as e:
                    _safe_unlink(tmp)
                    return DownloadResult(
                        hk_ticker=filing.hk_ticker,
                        outcome=DownloadOutcome.FAILED,
                        attempts=attempts,
                        error=f"File rename failed after 5 retries: {e}",
                    )
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
                    delay = _parse_retry_after_disc(resp)
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


def _safe_replace(src: Path, dst: Path, max_retries: int = 5, base_delay: float = 0.1) -> None:
    """Atomically replace dst with src, retrying on Windows file lock errors.

    Windows antivirus and other processes may lock a file briefly after write,
    causing PermissionError on os.replace(). This function retries with
    exponential backoff. If os.replace exhausts retries, falls back to
    shutil.copy2 + os.unlink which can work even when antivirus holds a
    read-lock on the source.
    """
    import shutil

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                if delay > 5.0:
                    delay = 5.0  # cap at 5s per attempt
                logger.debug(
                    "os.replace(%s, %s) failed (attempt %d/%d): %s, retrying in %.2fs",
                    src, dst, attempt + 1, max_retries, e, delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "os.replace(%s, %s) failed after %d attempts, trying copy+unlink",
                    src, dst, max_retries,
                )
    # Fallback: copy + unlink. Antivirus read-locks prevent os.replace but
    # allow reading (copy) and deletion after the scan completes.
    try:
        shutil.copy2(src, dst)
    except Exception:
        if last_error:
            raise last_error
        raise
    # Best-effort cleanup of the tmp source; a stale .tmp is harmless and
    # will be swept by sweep_orphan_tmp_files on the next run.
    try:
        os.unlink(src)
    except Exception:
        pass


