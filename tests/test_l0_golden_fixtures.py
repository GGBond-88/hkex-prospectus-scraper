"""Golden-fixture regression test for HKEX discovery.

Each fixture asserts the discovery client returns at least one Filing matching
the expected ticker within its declared date window. Excludes pre-2010
entries which are present only as smoke for "no false positives".
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from hk_ipo.l0.discovery import HKEXDiscoveryClient
from hk_ipo.l0.filter import is_english_prospectus

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "l0_known_ipos.json"
SPEC_RANGE_START = date(2010, 1, 1)


@pytest.fixture(scope="module")
def fixtures() -> list[dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data["fixtures"]


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("idx", range(10))
async def test_golden_fixture_discoverable(
    idx: int, fixtures: list[dict], tmp_path: Path,
) -> None:
    fx = fixtures[idx]
    listing_date = date.fromisoformat(fx["listing_date"])
    if listing_date < SPEC_RANGE_START:
        pytest.skip(f"{fx['hk_ticker']} pre-dates 2010 spec range; smoke-only entry")

    window_start = date.fromisoformat(fx["window_start"])

    client = HKEXDiscoveryClient(
        json_api_base="https://www1.hkexnews.hk/search/titlesearchservlet.do",
        html_search_base="https://www1.hkexnews.hk/search/titlesearch.xhtml",
        log_dir=tmp_path,
    )
    async with client:
        hits = [
            f async for f in client.list_filings(
                window_start, date.fromisoformat(fx["window_end"]),
            )
            if f.hk_ticker == fx["hk_ticker"] and is_english_prospectus(f)
        ]

    assert hits, (
        f"golden fixture {fx['hk_ticker']} ({fx['company_name_en']}) "
        f"not discovered in {fx['window_start']}..{fx['window_end']}; "
        "HKEX may have changed t1code/t2code or moved the doc."
    )
    assert any(h.doc_url.lower().endswith(fx["expected_url_suffix"]) for h in hits), \
        f"{fx['hk_ticker']}: no hit matched URL suffix {fx['expected_url_suffix']}"
