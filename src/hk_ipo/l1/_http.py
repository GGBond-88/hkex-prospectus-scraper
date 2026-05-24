"""Shared HTTP client with retry/pacing envelope for the L1 report+validation layer.

Clones the proven retry/pacing pattern from discovery.py:411-442 and 496-531:
  - 5xx + TransportError → tenacity exponential backoff (1-8s, 3 attempts)
  - 429 → 3-level escalating Retry-After backoff (30/60/120s fallback)
  - 429 after 3 rate-limit retries → RuntimeError
  - 1.5 s inter-request pacing via _last_request_time tracking
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

CACHE_TTL_DAYS = 7


def should_use_cache(cache_path: Path, *, force_refresh: bool = False) -> bool:
    """Return True if cache_path exists and is fresher than CACHE_TTL_DAYS."""
    if force_refresh:
        return False
    if not cache_path.exists():
        return False
    age_seconds = time.time() - cache_path.stat().st_mtime
    max_age = CACHE_TTL_DAYS * 86400
    return age_seconds < max_age


# ---------------------------------------------------------------------------
# Retry-After parser  (same logic as discovery.py:143-163)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ValidationHTTPClient
# ---------------------------------------------------------------------------

class ValidationHTTPClient:
    """Thin wrapper around httpx.AsyncClient with retry/pacing envelope
    identical to discovery.py's pattern."""

    def __init__(
        self,
        *,
        user_agent: str,
        per_request_max_attempts: int = 3,
        inter_request_sleep: float = 1.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._per_request_max_attempts = per_request_max_attempts
        self._inter_request_sleep = inter_request_sleep
        self._client = client
        self._owns_client = client is None
        self._last_request_time: float = 0.0

    # -- context manager -------------------------------------------------------

    async def __aenter__(self) -> "ValidationHTTPClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=httpx.Timeout(30.0, connect=30.0, read=30.0),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- public methods --------------------------------------------------------

    async def get(
        self, url: str, *, params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP GET with pacing and retry envelope."""
        await self._pace()
        try:
            return await self._request_with_retry("GET", url, params=params)
        finally:
            self._last_request_time = time.monotonic()

    async def post(
        self, url: str, *, data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP POST with pacing and retry envelope."""
        await self._pace()
        try:
            return await self._request_with_retry("POST", url, data=data)
        finally:
            self._last_request_time = time.monotonic()

    # -- internals -------------------------------------------------------------

    async def _pace(self) -> None:
        """Sleep if the inter-request gap has not yet elapsed."""
        now = time.monotonic()
        if self._last_request_time > 0:
            gap = now - self._last_request_time
            if gap < self._inter_request_sleep:
                await asyncio.sleep(self._inter_request_sleep - gap)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Unified retry envelope: 5xx → tenacity, 429 → 3-level backoff."""
        assert self._client is not None
        rate_limit_attempts = 0
        while True:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._per_request_max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.HTTPStatusError),
                ),
                reraise=True,
            ):
                with attempt:
                    if method == "GET":
                        r = await self._client.get(url, params=params)
                    else:
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
                        break  # reset inner tenacity, outer while loops

                    if r.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}",
                            request=r.request,
                            response=r,
                        )
                    return r
