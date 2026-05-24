"""Unit tests for hk_ipo.l1._http.

Tests cover:
  1. Successful GET returns response
  2. 5xx retries with exponential backoff
  3. 429 triggers Retry-After backoff, escalates through 3 levels
  4. 429 after 3 rate-limit retries raises RuntimeError
  5. Inter-request pacing: two rapid requests are spaced 1.5s apart
  6. should_use_cache(): fresh / missing / stale / force_refresh
  7. Context manager auto-creates and closes client
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from hk_ipo.l1._http import (
    CACHE_TTL_DAYS,
    ValidationHTTPClient,
    _parse_retry_after_disc,
    should_use_cache,
)


# ---------------------------------------------------------------------------
# _parse_retry_after_disc  (unit tests on the pure helper)
# ---------------------------------------------------------------------------

def test_parse_retry_after_integer_seconds() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "120"})
    assert _parse_retry_after_disc(resp) == 120.0


def test_parse_retry_after_whitespace_surrounding() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "  60  "})
    assert _parse_retry_after_disc(resp) == 60.0


def test_parse_retry_after_http_date() -> None:
    from email.utils import format_datetime

    from datetime import datetime, timedelta, timezone

    future_dt = datetime.now(timezone.utc) + timedelta(seconds=45)
    formatted = format_datetime(future_dt, usegmt=True)
    resp = httpx.Response(429, headers={"Retry-After": formatted})
    delta = _parse_retry_after_disc(resp)
    # Should be approximately 45 seconds (allow 2s tolerance).
    assert delta is not None
    assert 43 <= delta <= 47


def test_parse_retry_after_empty_header() -> None:
    resp = httpx.Response(429, headers={"Retry-After": ""})
    assert _parse_retry_after_disc(resp) is None


def test_parse_retry_after_missing_header() -> None:
    resp = httpx.Response(429)
    assert _parse_retry_after_disc(resp) is None


def test_parse_retry_after_non_numeric_garbage() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "not-a-number"})
    assert _parse_retry_after_disc(resp) is None


# ---------------------------------------------------------------------------
# should_use_cache
# ---------------------------------------------------------------------------

def test_should_use_cache_file_exists_and_fresh(tmp_path: Path) -> None:
    cache_path = tmp_path / "data.json"
    cache_path.write_text("{}")
    assert should_use_cache(cache_path) is True


def test_should_use_cache_file_missing(tmp_path: Path) -> None:
    cache_path = tmp_path / "nonexistent.json"
    assert should_use_cache(cache_path) is False


def test_should_use_cache_file_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "old.json"
    cache_path.write_text("{}")
    stale_ts = time.time() - (CACHE_TTL_DAYS * 86400 + 3600)
    os.utime(str(cache_path), (stale_ts, stale_ts))
    assert should_use_cache(cache_path) is False


def test_should_use_cache_force_refresh(tmp_path: Path) -> None:
    cache_path = tmp_path / "fresh.json"
    cache_path.write_text("{}")
    assert should_use_cache(cache_path, force_refresh=True) is False


# ---------------------------------------------------------------------------
# Successful GET / POST
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_get_returns_successful_response() -> None:
    """A 200 response is returned unchanged."""
    base = "https://example.test"
    respx.get(f"{base}/endpoint").mock(
        return_value=httpx.Response(200, text="payload")
    )

    async with ValidationHTTPClient(user_agent="test/1.0") as client:
        resp = await client.get(f"{base}/endpoint")
        assert resp.status_code == 200
        assert resp.text == "payload"


@respx.mock
@pytest.mark.asyncio
async def test_post_returns_successful_response() -> None:
    """A 200 POST response is returned unchanged."""
    base = "https://example.test"
    respx.post(f"{base}/form").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with ValidationHTTPClient(user_agent="test/1.0") as client:
        resp = await client.post(f"{base}/form", data={"key": "val"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5xx retries with exponential backoff
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_5xx_retries_and_eventually_succeeds() -> None:
    """Two 500s then a 200 = 3 total attempts (tenacity retries on 5xx)."""
    base = "https://example.test"
    route = respx.get(f"{base}/flaky").mock(side_effect=[
        httpx.Response(500, text="err1"),
        httpx.Response(503, text="err2"),
        httpx.Response(200, text="ok"),
    ])

    with patch("asyncio.sleep", new_callable=AsyncMock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", per_request_max_attempts=3,
        ) as client:
            resp = await client.get(f"{base}/flaky")
            assert resp.status_code == 200

    assert route.call_count == 3, f"expected 3 attempts, got {route.call_count}"


@respx.mock
@pytest.mark.asyncio
async def test_5xx_exhausts_retries_and_raises() -> None:
    """All attempts return 5xx — the last HTTPStatusError propagates."""
    base = "https://example.test"
    respx.get(f"{base}/doomed").mock(
        return_value=httpx.Response(500, text="dead")
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", per_request_max_attempts=2,
        ) as client:
            with pytest.raises(httpx.HTTPStatusError, match="500"):
                await client.get(f"{base}/doomed")


# ---------------------------------------------------------------------------
# 429 handling — Retry-After, escalation, exhaustion
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_429_triggers_retry_after_and_then_succeeds() -> None:
    """A 429 with Retry-After: 1 sleeps 1s, then the retry succeeds."""
    base = "https://example.test"
    route = respx.get(f"{base}/throttled").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, text="ok"),
    ])

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(user_agent="test/1.0") as client:
            resp = await client.get(f"{base}/throttled")
            assert resp.status_code == 200

    # The only sleep should be from the 429 Retry-After handler (1 second).
    sleep_429_calls = [
        c for c in sleep_mock.call_args_list
        if c[0] == (1.0,)
    ]
    assert len(sleep_429_calls) == 1, f"expected one sleep(1.0), got {sleep_mock.call_args_list}"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_429_escalates_through_three_levels() -> None:
    """Three consecutive 429s each get escalating fallback delays (30/60/120)."""
    base = "https://example.test"
    respx.get(f"{base}/overloaded").mock(
        return_value=httpx.Response(429)  # no Retry-After header
    )

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock), pytest.raises(RuntimeError, match="429"):
        async with ValidationHTTPClient(user_agent="test/1.0") as client:
            await client.get(f"{base}/overloaded")

    # Three escalating sleeps: 30.0, 60.0, 120.0 (no Retry-After → fallback).
    assert any(c[0] == (30.0,) for c in sleep_mock.call_args_list), sleep_mock.call_args_list
    assert any(c[0] == (60.0,) for c in sleep_mock.call_args_list), sleep_mock.call_args_list
    assert any(c[0] == (120.0,) for c in sleep_mock.call_args_list), sleep_mock.call_args_list


@respx.mock
@pytest.mark.asyncio
async def test_429_after_3_rate_limit_retries_raises_runtime_error() -> None:
    """The 4th consecutive 429 raises RuntimeError (3 retries exhausted)."""
    base = "https://example.test"
    route = respx.get(f"{base}/perma_429").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", per_request_max_attempts=3,
        ) as client:
            with pytest.raises(RuntimeError, match="429.*after 3"):
                await client.get(f"{base}/perma_429")

    # 4 HTTP calls: 3 retried + 1 that triggers the RuntimeError.
    assert route.call_count == 4


@respx.mock
@pytest.mark.asyncio
async def test_429_on_post_also_uses_retry_envelope() -> None:
    """POST requests must use the same 429 handling as GET."""
    base = "https://example.test"
    route = respx.post(f"{base}/search").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, text="results"),
    ])

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(user_agent="test/1.0") as client:
            resp = await client.post(f"{base}/search", data={"q": "test"})
            assert resp.status_code == 200

    assert route.call_count == 2


# ---------------------------------------------------------------------------
# Inter-request pacing
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_first_request_does_not_sleep() -> None:
    """The very first request after initialization skips pacing."""
    base = "https://example.test"
    respx.get(f"{base}/first").mock(
        return_value=httpx.Response(200, text="ok")
    )

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", inter_request_sleep=1.5,
        ) as client:
            await client.get(f"{base}/first")

    # No pacing sleep for the first request (_last_request_time = 0).
    # Since first request succeeds immediately, no tenacity sleeps either.
    assert sleep_mock.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_back_to_back_requests_honor_pacing_gap() -> None:
    """When _last_request_time is recent, pacing should sleep the remainder."""
    base = "https://example.test"
    respx.get(f"{base}/endpoint").mock(
        return_value=httpx.Response(200, text="ok")
    )

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", inter_request_sleep=1.5,
        ) as client:
            # Simulate a request that finished 0.5s ago.
            client._last_request_time = time.monotonic() - 0.5
            await client.get(f"{base}/endpoint")

    # Gap = 0.5 s, need 1.5 - 0.5 = 1.0 s more.  Allow small float tolerance.
    assert sleep_mock.call_count == 1
    called_with = sleep_mock.call_args[0][0]
    assert called_with == pytest.approx(1.0, abs=0.1), (
        f"expected sleep ~1.0, got {called_with}"
    )


@respx.mock
@pytest.mark.asyncio
async def test_pacing_skips_when_gap_already_satisfied() -> None:
    """When the gap since _last_request_time already exceeds the pacing interval,
    no extra sleep is needed."""
    base = "https://example.test"
    respx.get(f"{base}/endpoint").mock(
        return_value=httpx.Response(200, text="ok")
    )

    sleep_mock = AsyncMock()
    with patch("asyncio.sleep", sleep_mock):
        async with ValidationHTTPClient(
            user_agent="test/1.0", inter_request_sleep=1.5,
        ) as client:
            # Simulate a request that finished 2.0s ago (gap > 1.5).
            client._last_request_time = time.monotonic() - 2.0
            await client.get(f"{base}/endpoint")

    # No pacing sleep needed — the gap already satisfied the constraint.
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_manager_auto_creates_client() -> None:
    """When no client is supplied, __aenter__ creates one with our User-Agent."""
    async with ValidationHTTPClient(user_agent="my-ua/1.0") as client:
        assert client._client is not None
        assert client._owns_client is True
        assert client._client.headers["User-Agent"] == "my-ua/1.0"


@pytest.mark.asyncio
async def test_context_manager_closes_owned_client() -> None:
    """After __aexit__, an owned client is cleaned up."""
    client = ValidationHTTPClient(user_agent="test/1.0")
    async with client:
        pass  # just enter and exit
    assert client._client is None


@pytest.mark.asyncio
async def test_supplied_client_is_not_closed() -> None:
    """When the caller supplies a client, __aexit__ must not close it."""
    supplied = httpx.AsyncClient()
    client = ValidationHTTPClient(user_agent="test/1.0", client=supplied)
    assert client._owns_client is False

    async with client as wrapper:
        assert wrapper._client is supplied

    # The caller's client must remain open.
    assert not supplied.is_closed
    await supplied.aclose()


@pytest.mark.asyncio
async def test_timeout_defaults() -> None:
    """Owned client uses 30s connect + 30s read timeout."""
    async with ValidationHTTPClient(user_agent="test/1.0") as client:
        timeout = client._client.timeout
        # httpx.Timeout with connect/read both set to 30
        assert timeout.connect == 30.0
        assert timeout.read == 30.0


@respx.mock
@pytest.mark.asyncio
async def test_client_respects_user_agent_header() -> None:
    """The configured User-Agent is attached to every request."""
    base = "https://example.test"
    route = respx.get(f"{base}/ua-test").mock(
        return_value=httpx.Response(200),
    )

    async with ValidationHTTPClient(user_agent="custom-agent/2.0") as client:
        await client.get(f"{base}/ua-test")

    sent_ua = route.calls[0].request.headers.get("User-Agent")
    assert sent_ua == "custom-agent/2.0"
