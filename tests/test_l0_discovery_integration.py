"""Integration test: replay recorded HKEX responses via pytest-recording.

This test runs offline on every CI invocation. To refresh the cassette, see
tests/cassettes/README.md.
"""
from __future__ import annotations

import pathlib
from datetime import date
from pathlib import Path

import pytest

from hk_ipo.l0.discovery import HKEXDiscoveryClient
from hk_ipo.l0.models import Filing


CASSETTE_NAME: str = "test_l0_discovery_replays_recorded_hkex_window.yaml"


def _flatten_vcr_path(path: str) -> str:
    """Return a flat cassette filename regardless of VCR's default subdirectory."""
    return str(pathlib.Path(path).parent / CASSETTE_NAME)


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_discovery_replays_recorded_hkex_window(tmp_path: Path) -> None:
    """Replay one real HKEX month and assert filings are parsed correctly."""
    client = HKEXDiscoveryClient(
        json_api_base="https://www1.hkexnews.hk/search/titlesearchservlet.do",
        html_search_base="https://www1.hkexnews.hk/search/titlesearch.xhtml",
        log_dir=tmp_path,
        per_request_max_attempts=1,
    )
    async with client:
        filings = [f async for f in client.list_filings(date(2024, 1, 1), date(2024, 1, 31))]

    assert len(filings) > 0, "No filings were returned -- is the cassette missing or empty?"

    # The exact count depends on the recorded cassette, but every filing must
    # parse into a well-formed Filing.
    for f in filings:
        assert isinstance(f, Filing)
        assert f.hk_ticker.isdigit() and len(f.hk_ticker) == 5
        assert f.doc_url.startswith("https://www1.hkexnews.hk/")
        assert f.market in ("MB", "GEM")
        assert f.publish_date.tzinfo is not None


@pytest.fixture
def vcr_config() -> dict:
    return {
        "filter_headers": ["authorization", "cookie", "set-cookie"],
        "decode_compressed_response": True,
        "record_mode": "none",  # never hit the network in CI
        "cassette_library_dir": str(Path("tests/cassettes")),
        "path_transformer": _flatten_vcr_path,
    }
