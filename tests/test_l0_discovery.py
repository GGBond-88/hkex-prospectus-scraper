"""Unit tests for hk_ipo.l0.discovery (httpx mocked via respx)."""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from hk_ipo.l0.discovery import (
    HKEXDiscoveryClient,
    iter_monthly_windows,
)
from hk_ipo.l0.models import Filing


# ---------- iter_monthly_windows ----------

def test_iter_monthly_windows_basic() -> None:
    windows = list(iter_monthly_windows(date(2024, 1, 15), date(2024, 3, 10)))
    assert windows == [
        (date(2024, 1, 1), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),  # 2024 is a leap year
        (date(2024, 3, 1), date(2024, 3, 31)),
    ]


def test_iter_monthly_windows_single_month() -> None:
    windows = list(iter_monthly_windows(date(2024, 7, 1), date(2024, 7, 31)))
    assert windows == [(date(2024, 7, 1), date(2024, 7, 31))]


def test_iter_monthly_windows_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        list(iter_monthly_windows(date(2024, 2, 1), date(2024, 1, 1)))


def test_iter_monthly_windows_year_boundary() -> None:
    windows = list(iter_monthly_windows(date(2023, 12, 5), date(2024, 1, 10)))
    assert windows == [
        (date(2023, 12, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 1, 31)),
    ]


# ---------- JSON path ----------

_SAMPLE_JSON = {
    "hits": [
        {
            "DOC_ID": "2024010100001",
            "STOCK_CODE": "9999",
            "STOCK_NAME_EN": "Test Holdings Limited",
            "STOCK_NAME_C": "测试控股有限公司",
            "TITLE": "GLOBAL OFFERING",
            "DATE_TIME": "2024-01-15 08:30:00",
            "T1_CODE": "40000",
            "T2_CODE": "40100",
            "MARKET": "SEHK",
            "LANGUAGE_CD": "E",
            "FILE_LINK": "/listedco/listconews/sehk/2024/0115/2024010100001_e.pdf",
        }
    ],
    "total": 1,
    "page": 1,
    "pageSize": 100,
}


@respx.mock
@pytest.mark.asyncio
async def test_discovery_json_yields_filings(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearchservlet.do"
    respx.get(base).mock(return_value=httpx.Response(200, json=_SAMPLE_JSON))

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/search/titlesearch.xhtml",
        log_dir=tmp_path,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]

    assert len(filings) == 1
    f = filings[0]
    assert isinstance(f, Filing)
    assert f.hk_ticker == "09999"
    assert f.doc_id == "2024010100001"
    assert f.doc_url.endswith("2024010100001_e.pdf")
    assert f.doc_url.startswith("https://www1.hkexnews.hk/")  # absolute URL
    assert f.doc_type == "Prospectus"
    assert f.market == "MB"
    assert f.language == "en"
    assert f.is_final is True
    assert f.company_name_en == "Test Holdings Limited"


@respx.mock
@pytest.mark.asyncio
async def test_discovery_json_persists_raw_response(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearchservlet.do"
    respx.get(base).mock(return_value=httpx.Response(200, json=_SAMPLE_JSON))

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/x",
        log_dir=tmp_path,
    )
    async with client:
        async for _ in client.list_filings(date(2024, 1, 1), date(2024, 1, 31)):
            pass

    archives = list((tmp_path / "discovery").glob("2024-01*.json"))
    assert len(archives) >= 1
    parsed = json.loads(archives[0].read_text(encoding="utf-8"))
    assert parsed["hits"][0]["DOC_ID"] == "2024010100001"


@respx.mock
@pytest.mark.asyncio
async def test_discovery_json_derives_doc_type_from_market(tmp_path: Path) -> None:
    """SEHK -> Prospectus, GEM -> Listing Document - GEM (PR-003 fix)."""
    base = "https://example.test/search/titlesearchservlet.do"
    multi = {
        "hits": [
            {
                "DOC_ID": "2024010100001", "STOCK_CODE": "0001",
                "STOCK_NAME_EN": "MB Co", "TITLE": "Prospectus",
                "DATE_TIME": "2024-01-15 08:30:00",
                "T1_CODE": "40000", "T2_CODE": "40100",
                "MARKET": "SEHK", "LANGUAGE_CD": "E",
                "FILE_LINK": "/sehk/e.pdf",
            },
            {
                "DOC_ID": "2024010200002", "STOCK_CODE": "0002",
                "STOCK_NAME_EN": "GEM Co", "TITLE": "Listing Document",
                "DATE_TIME": "2024-01-22 09:00:00",
                "T1_CODE": "40000", "T2_CODE": "40100",
                "MARKET": "GEM", "LANGUAGE_CD": "E",
                "FILE_LINK": "/gem/e.pdf",
            },
        ],
        "total": 2, "page": 1, "pageSize": 100,
    }
    respx.get(base).mock(return_value=httpx.Response(200, json=multi))

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/x",
        log_dir=tmp_path,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]

    assert len(filings) == 2
    mb, gem = filings
    assert mb.market == "MB" and mb.doc_type == "Prospectus"
    assert gem.market == "GEM" and gem.doc_type == "Listing Document - GEM"


# ---------- HTML fallback ----------

_HTML_FIXTURE = """\
<table><tr class="row">
  <td class="release-date">2024-02-10 09:00:00</td>
  <td class="stock-code">9997</td>
  <td class="stock-name">HTML Fallback Holdings Limited</td>
  <td class="title"><a href="/listedco/listconews/sehk/2024/0210/2024021000003_e.pdf">Global Offering</a></td>
  <td class="doc-type">Prospectus</td>
  <td class="market">SEHK</td>
</tr></table>
"""


@respx.mock
@pytest.mark.asyncio
async def test_discovery_falls_back_to_html_when_json_500s(tmp_path: Path) -> None:
    json_base = "https://example.test/search/titlesearchservlet.do"
    html_base = "https://example.test/search/titlesearch.xhtml"
    respx.get(json_base).mock(return_value=httpx.Response(500))
    respx.get(html_base).mock(return_value=httpx.Response(200, text=_HTML_FIXTURE))

    client = HKEXDiscoveryClient(
        json_api_base=json_base, html_search_base=html_base, log_dir=tmp_path,
        per_request_max_attempts=2,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 2, 1), date(2024, 2, 29))]

    assert len(filings) == 1
    assert filings[0].hk_ticker == "09997"
    assert filings[0].doc_url.endswith("2024021000003_e.pdf")
    assert filings[0].market == "MB"


@respx.mock
@pytest.mark.asyncio
async def test_discovery_one_bad_window_does_not_halt(tmp_path: Path) -> None:
    """First window 500s on both JSON and HTML; subsequent window still yields."""
    json_base = "https://example.test/search/titlesearchservlet.do"
    html_base = "https://example.test/search/titlesearch.xhtml"

    def json_side(request: httpx.Request) -> httpx.Response:
        from_param = request.url.params.get("from")
        if from_param == "20240101":
            return httpx.Response(500)
        return httpx.Response(200, json=_SAMPLE_JSON)

    def html_side(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    respx.get(json_base).mock(side_effect=json_side)
    respx.get(html_base).mock(side_effect=html_side)

    client = HKEXDiscoveryClient(
        json_api_base=json_base, html_search_base=html_base, log_dir=tmp_path,
        per_request_max_attempts=2,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 2, 29))]

    # Jan failed, Feb succeeded.
    assert len(filings) == 1
    assert client.failed_windows and client.failed_windows[0]["start"] == "2024-01-01"


# ---------- Pagination ----------


@respx.mock
@pytest.mark.asyncio
async def test_discovery_paginates_until_total_reached(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearchservlet.do"

    def make_page(n: int, total: int) -> dict:
        return {
            "hits": [
                {
                    "DOC_ID": f"20240101000{n}",
                    "STOCK_CODE": f"{n:04d}",
                    "STOCK_NAME_EN": "X",
                    "TITLE": "Global Offering",
                    "DATE_TIME": "2024-01-15 08:30:00",
                    "T1_CODE": "40000", "T2_CODE": "40100",
                    "MARKET": "SEHK", "LANGUAGE_CD": "E",
                    "FILE_LINK": f"/p/{n}_e.pdf",
                }
            ],
            "total": total, "page": n, "pageSize": 1,
        }

    def side(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=make_page(page, total=3))

    respx.get(base).mock(side_effect=side)

    client = HKEXDiscoveryClient(
        json_api_base=base, html_search_base="https://example.test/x",
        log_dir=tmp_path, page_size=1,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]
    assert len(filings) == 3
    assert {f.hk_ticker for f in filings} == {"00001", "00002", "00003"}


# ---------- Inter-window pacing ----------


@respx.mock
@pytest.mark.asyncio
async def test_discovery_sleeps_between_windows(tmp_path: Path,
                                                monkeypatch) -> None:
    """3 windows must trigger 2 sleeps when inter_window_sleep > 0."""
    base = "https://example.test/search/titlesearchservlet.do"
    respx.get(base).mock(
        return_value=httpx.Response(200, json=_SAMPLE_JSON),
    )
    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/x",
        log_dir=tmp_path,
        inter_window_sleep=1.5,
    )
    async with client:
        filings = [f async for f in client.list_filings(
            date(2024, 1, 1), date(2024, 3, 31),
        )]
    assert len(filings) == 3  # one per window
    assert len(sleeps) == 2   # 3 windows -> 2 gaps
    for s in sleeps:
        assert s == 1.5


# ---------- 429 Rate Limiting ----------


@respx.mock
@pytest.mark.asyncio
async def test_discovery_429_respects_retry_after_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After: must sleep the header value, then retry successfully."""
    base = "https://example.test/search/titlesearchservlet.do"
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("hk_ipo.l0.discovery.asyncio.sleep", fake_sleep)

    respx.get(base).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=_SAMPLE_JSON),
    ])

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/x",
        log_dir=tmp_path,
        per_request_max_attempts=3,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]

    assert len(filings) == 1
    assert len(sleeps) == 1
    assert sleeps[0] == 7.0


@respx.mock
@pytest.mark.asyncio
async def test_discovery_429_fallback_backoff_without_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 without Retry-After header: fall back to 30s/60s/120s fixed sequence."""
    base = "https://example.test/search/titlesearchservlet.do"
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("hk_ipo.l0.discovery.asyncio.sleep", fake_sleep)

    respx.get(base).mock(side_effect=[
        httpx.Response(429),   # no header -> 30s
        httpx.Response(429),   # no header -> 60s
        httpx.Response(200, json=_SAMPLE_JSON),
    ])

    client = HKEXDiscoveryClient(
        json_api_base=base,
        html_search_base="https://example.test/x",
        log_dir=tmp_path,
        per_request_max_attempts=3,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]

    assert len(filings) == 1
    assert sleeps == [30.0, 60.0]
