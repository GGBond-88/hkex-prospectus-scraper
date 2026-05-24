"""Unit tests for hk_ipo.l0.discovery.

MVP-1 rewrite (2026-05-22): the discovery client now uses the live HKEX
flow (partial.do JSONP for ticker -> stockId, then POST titlesearch.xhtml
for the date-window search). Old JSON-API tests against the retired
titlesearchservlet.do endpoint were removed — that code path no longer
exists in production. The legacy `_fetch_json_window` method remains in
discovery.py only for code archaeology and is not exercised here.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from hk_ipo.l0.discovery import (
    HKEXDiscoveryClient,
    _parse_filings_from_search_html,
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


# ---------- HTML row parser (pure function) ----------

XIAOMI_FIXTURE = (
    Path(__file__).parent / "fixtures" / "xiaomi_search_response.html"
).read_text(encoding="utf-8")


def test_parser_yields_only_ipo_rows_from_real_response() -> None:
    """Real Xiaomi capture (2018-06 to 2018-07, 19 rows total)."""
    filings = list(_parse_filings_from_search_html(
        XIAOMI_FIXTURE, pdf_base="https://www1.hkexnews.hk",
    ))
    # Out of 19 rows, only IPO-listing rows survive parser: 2 Listing Doc
    # PDFs (Global Offering + White App Form -- both .pdf, headline =
    # Listing Documents -) + 1 PHIP (Application Proofs prefix). The Multi-
    # Files HTM Global Offering index is dropped (.htm). The Formal Notice
    # PDF is dropped (headline = Announcements and Notices). And 12 other
    # A&N / NDDR / Constitutional rows are dropped.
    tickers = {f.hk_ticker for f in filings}
    assert tickers == {"01810"}
    titles = sorted(f.doc_title for f in filings)
    assert "GLOBAL OFFERING" in titles
    assert "White Application Form" in titles
    assert "PHIP (1st submission)" in titles


def test_parser_marks_global_offering_as_final_but_application_form_not() -> None:
    filings = list(_parse_filings_from_search_html(
        XIAOMI_FIXTURE, pdf_base="https://www1.hkexnews.hk",
    ))
    by_title = {f.doc_title: f for f in filings}
    # The canonical prospectus row is is_final=True.
    assert by_title["GLOBAL OFFERING"].is_final is True
    # Application Form lives under "Listing Documents - " headline but is
    # not the prospectus -- parser sets is_final=False so filter.py
    # classifies it as wrong_doc_type.
    assert by_title["White Application Form"].is_final is False
    # PHIP lives under "Application Proofs and ..." headline -> is_final=False.
    assert by_title["PHIP (1st submission)"].is_final is False


def test_parser_resolves_absolute_url_with_pdf_base() -> None:
    filings = list(_parse_filings_from_search_html(
        XIAOMI_FIXTURE, pdf_base="https://example.com",
    ))
    prospectus = next(f for f in filings if f.doc_title == "GLOBAL OFFERING")
    assert prospectus.doc_url == (
        "https://example.com/listedco/listconews/sehk/2018/0625/ltn20180625033.pdf"
    )


def test_parser_drops_non_ipo_headlines() -> None:
    """Rows like A&N, NDDR, Constitutional should not be yielded at all."""
    filings = list(_parse_filings_from_search_html(
        XIAOMI_FIXTURE, pdf_base="https://www1.hkexnews.hk",
    ))
    # No row should have a non-IPO-related title slipped through. We don't
    # see "DATE OF BOARD MEETING" or "Memorandum and Articles of Association"
    # because their headlines don't start with "Listing Documents -" or
    # "Application Proofs".
    titles_lower = [f.doc_title.lower() for f in filings]
    assert not any("board meeting" in t for t in titles_lower)
    assert not any("memorandum" in t for t in titles_lower)
    assert not any("disclosure return" in t for t in titles_lower)


def test_parser_drops_htm_multifile_index_rows() -> None:
    filings = list(_parse_filings_from_search_html(
        XIAOMI_FIXTURE, pdf_base="https://www1.hkexnews.hk",
    ))
    # No filing should reference a .htm URL.
    assert all(f.doc_url.endswith(".pdf") for f in filings)


# ---------- list_filings (HTML POST integration via respx) ----------

_MINIMAL_SEARCH_HTML = """<!DOCTYPE html><html><body>
<div class="title-search-info-footer clearfix">
  <div class="total-records">Total records found: 15</div>
</div>
<table><tbody>
<tr>
  <td class="release-time"><span class="mobile-list-heading">Release Time: </span>15/01/2024 08:30</td>
  <td class="stock-short-code"><span class="mobile-list-heading">Stock Code: </span>09999</td>
  <td class="stock-short-name"><span class="mobile-list-heading">Stock Short Name: </span>Test Co</td>
  <td>
    <div class="headline">Listing Documents - [Offer for Subscription]</div>
    <div class="doc-link">
      <a href="/listedco/listconews/sehk/2024/0115/ltn20240115001.pdf">GLOBAL OFFERING</a>
      (<span class="attachment_filesize">3500KB</span>)
    </div>
  </td>
</tr>
</tbody></table>
</body></html>
"""


@respx.mock
@pytest.mark.asyncio
async def test_list_filings_posts_to_titlesearch_with_form_data(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearch.xhtml"
    route = respx.post(base).mock(
        return_value=httpx.Response(200, text=_MINIMAL_SEARCH_HTML),
    )

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base=base,
        log_dir=tmp_path,
    )
    async with client:
        filings = [f async for f in client.list_filings(
            date(2024, 1, 1), date(2024, 1, 31),
        )]

    # Bug #1 fix: 4 POSTs per window (one per IPO t2code subtype). Each resp
    # contains the same single filing row, but dedup collapses them to 1.
    assert len(filings) == 1
    f = filings[0]
    assert isinstance(f, Filing)
    assert f.hk_ticker == "09999"
    assert f.doc_title == "GLOBAL OFFERING"
    assert f.doc_url.endswith("ltn20240115001.pdf")
    assert f.is_final is True
    assert route.call_count == 4
    # Spot-check: the first POST body should use t2code=30500 (Introduction).
    req = route.calls[0].request
    body = req.content.decode("ascii")
    assert "from=20240101" in body, body
    assert "to=20240131" in body, body
    assert "t1code=30000" in body, body
    assert "t2code=30500" in body, body
    assert "searchType=1" in body, body
    assert "market=SEHK" in body, body


@respx.mock
@pytest.mark.asyncio
async def test_list_filings_one_bad_window_does_not_halt(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearch.xhtml"
    # First window: first subtype POST 500s x3 (exhausts retries), window fails.
    # Second window: 4 subtype POSTs all succeed.
    responses = [
        httpx.Response(500, text=""),
        httpx.Response(500, text=""),
        httpx.Response(500, text=""),
    ] + [httpx.Response(200, text=_MINIMAL_SEARCH_HTML)] * 4
    respx.post(base).mock(side_effect=responses)

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base=base,
        log_dir=tmp_path,
    )
    async with client:
        filings = [f async for f in client.list_filings(
            date(2024, 1, 1), date(2024, 2, 29),
        )]

    # Second window's filing must still be yielded (deduped across 4 POSTs → 1).
    assert len(filings) == 1
    # And the failed window must have been logged.
    failed_log = tmp_path / "failed_windows.json"
    assert failed_log.exists()


@respx.mock
@pytest.mark.asyncio
async def test_list_filings_archives_html_response(tmp_path: Path) -> None:
    base = "https://example.test/search/titlesearch.xhtml"
    respx.post(base).mock(return_value=httpx.Response(200, text=_MINIMAL_SEARCH_HTML))

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base=base,
        log_dir=tmp_path,
    )
    async with client:
        async for _ in client.list_filings(date(2024, 1, 1), date(2024, 1, 31)):
            pass

    archives = list((tmp_path / "discovery").glob("2024-01-01*.html"))
    assert len(archives) == 4, f"expected 4 archives (one per t2code), found {archives}"
    assert any("t2_30500" in a.name for a in archives)
    assert any("t2_30700" in a.name for a in archives)


# ---------- lookup_stock_id (partial.do JSONP) ----------

_PARTIAL_XIAOMI_JSONP = (
    'callback([{"stockId":190371,"code":"01810","name":"XIAOMI-W"},'
    '{"stockId":1000265257,"code":"18106","name":"UBSANDS@EP2606A"},'
    '{"stockId":1000293010,"code":"61810","name":"HS#POMRTRP2808E"},'
    '{"stockId":1000195151,"code":"81810","name":"XIAOMI-WR"}]);'
)


@respx.mock
@pytest.mark.asyncio
async def test_lookup_stock_id_strips_jsonp_and_matches_exact_ticker(
    tmp_path: Path,
) -> None:
    partial_base = "https://example.test/search/partial.do"
    respx.get(partial_base).mock(
        return_value=httpx.Response(
            200, text=_PARTIAL_XIAOMI_JSONP,
            headers={"Content-Type": "application/javascript;charset=utf-8"},
        ),
    )

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base="https://example.test/search/titlesearch.xhtml",
        partial_lookup_base=partial_base,
        log_dir=tmp_path,
    )
    async with client:
        # 01810 must resolve to 190371, not to 81810's stockId.
        stock_id = await client.lookup_stock_id("01810")
        assert stock_id == 190371
        # The 5-digit padded form should produce the same answer.
        assert await client.lookup_stock_id("1810") == 190371


@respx.mock
@pytest.mark.asyncio
async def test_lookup_stock_id_returns_none_when_no_match(tmp_path: Path) -> None:
    partial_base = "https://example.test/search/partial.do"
    respx.get(partial_base).mock(
        return_value=httpx.Response(200, text="callback([]);"),
    )

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base="https://example.test/search/titlesearch.xhtml",
        partial_lookup_base=partial_base,
        log_dir=tmp_path,
    )
    async with client:
        assert await client.lookup_stock_id("99999") is None


@respx.mock
@pytest.mark.asyncio
async def test_lookup_stock_id_caches_results(tmp_path: Path) -> None:
    partial_base = "https://example.test/search/partial.do"
    route = respx.get(partial_base).mock(
        return_value=httpx.Response(200, text=_PARTIAL_XIAOMI_JSONP),
    )

    client = HKEXDiscoveryClient(
        json_api_base="https://example.test/legacy",
        html_search_base="https://example.test/search/titlesearch.xhtml",
        partial_lookup_base=partial_base,
        log_dir=tmp_path,
    )
    async with client:
        await client.lookup_stock_id("01810")
        await client.lookup_stock_id("01810")
        await client.lookup_stock_id("01810")
    assert route.call_count == 1, "cached lookups must not re-hit partial.do"
