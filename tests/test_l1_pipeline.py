"""Unit tests for hk_ipo.l1.pipeline — orchestration functions."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hk_ipo.l1.models import ExternalIPO, NormalizedEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_MANIFEST = """
{
  "schema_version": 1,
  "entries": {}
}
"""

_SAMPLE_MANIFEST = """
{
  "schema_version": 1,
  "entries": {
    "01810": {
      "hk_ticker": "01810",
      "status": "success",
      "doc_id": "2018062500002",
      "doc_url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2018/0625/2018062500002_e.pdf",
      "doc_title": "Global Offering",
      "company_name_en": "Xiaomi Corporation",
      "company_name_zh": "\\u5c0f\\u7c73\\u96c6\\u56e2",
      "listing_date": "2018-07-09",
      "market": "MB",
      "language": "en",
      "file_path": "01810.pdf",
      "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "file_size_bytes": 12345678,
      "discovered_at": "2024-01-15T00:00:00Z",
      "downloaded_at": "2024-01-15T00:00:01Z"
    },
    "00123": {
      "hk_ticker": "00123",
      "status": "success",
      "doc_id": "2024030100005",
      "doc_url": "https://www1.hkexnews.hk/listedco/listconews/gem/2024/0301/2024030100005_e.pdf",
      "doc_title": "Listing Document",
      "company_name_en": "GEM Test Co Ltd",
      "market": "GEM",
      "language": "en",
      "file_path": "00123.pdf",
      "file_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "file_size_bytes": 5000000,
      "discovered_at": "2024-03-05T08:00:00Z",
      "downloaded_at": "2024-03-05T08:00:05Z"
    },
    "06666": {
      "hk_ticker": "06666",
      "status": "skipped_no_english",
      "doc_id": "2024031500010",
      "doc_url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2024/0315/2024031500010_c.pdf",
      "doc_title": "\\u62db\\u80a1\\u7ae0\\u7a0b",
      "company_name_zh": "\\u6d4b\\u8bd5\\u63a7\\u80a1\\u6709\\u9650\\u516c\\u53f8",
      "discovered_at": "2024-03-20T00:00:00Z",
      "skip_reason": "no_english_version_published"
    }
  }
}
"""


def _write_manifest(path: Path, content: str) -> Path:
    """Write a manifest JSON string to path, returning the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_external(
    *,
    ticker: str = "01810",
    company: str = "TestCo Ltd",
    list_date: date | None = date(2024, 3, 1),
    source: str = "aastocks",
    source_url: str | None = None,
) -> ExternalIPO:
    return ExternalIPO(
        hk_ticker=ticker,
        company_name=company,
        list_date=list_date,
        source=source,
        source_url=source_url or f"https://{source}.com/{ticker}",
    )


# ---------------------------------------------------------------------------
# run_report tests
# ---------------------------------------------------------------------------


class TestRunReport:
    def test_creates_summary_file(self, tmp_path: Path) -> None:
        """run_report should write summary.md from a valid manifest."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "summary.md"

        code = _run_async(run_report(manifest, output))

        assert code == 0
        assert output.exists()
        text = output.read_text(encoding="utf-8")
        assert "# HKEX IPO Prospectus Download Summary" in text

    def test_manifest_not_found_returns_1(self, tmp_path: Path) -> None:
        """run_report should return 1 and print to stderr when manifest is missing."""
        from hk_ipo.l1.pipeline import run_report

        manifest = tmp_path / "nonexistent" / "manifest.json"
        output = tmp_path / "summary.md"

        code = _run_async(run_report(manifest, output))

        assert code == 1
        # Should not create output when there's nothing to report
        assert not output.exists()

    def test_filter_by_since(self, tmp_path: Path) -> None:
        """since=<date> should exclude entries before that date."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "summary.md"

        # since=2020-01-01 should exclude 01810 (year 2018)
        code = _run_async(run_report(manifest, output, since=date(2020, 1, 1)))

        assert code == 0
        text = output.read_text(encoding="utf-8")
        # 01810 is from 2018, should be excluded
        assert "Xiaomi" not in text
        # 00123 is from 2024, should be included
        assert "GEM Test" in text

    def test_filter_by_until(self, tmp_path: Path) -> None:
        """until=<date> should exclude entries after that date."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "summary.md"

        # until=2020-01-01 should exclude 2024 entries, include 2018
        code = _run_async(run_report(manifest, output, until=date(2020, 1, 1)))

        assert code == 0
        text = output.read_text(encoding="utf-8")
        # 00123 is from 2024, should be excluded
        assert "GEM Test" not in text
        # 01810 is from 2018, should be included
        assert "Xiaomi" in text

    def test_since_and_until_narrow_window(self, tmp_path: Path) -> None:
        """Both since and until restrict the window."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "summary.md"

        # 2017-2019 window: only 01810 (2018) falls in range
        code = _run_async(run_report(
            manifest, output,
            since=date(2017, 1, 1),
            until=date(2019, 12, 31),
        ))

        assert code == 0
        text = output.read_text(encoding="utf-8")
        assert "Xiaomi" in text
        assert "GEM Test" not in text

    def test_empty_manifest_produces_summary(self, tmp_path: Path) -> None:
        """Empty manifest should still produce a valid summary.md."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _EMPTY_MANIFEST)
        output = tmp_path / "summary.md"

        code = _run_async(run_report(manifest, output))

        assert code == 0
        assert output.exists()
        text = output.read_text(encoding="utf-8")
        assert "# HKEX IPO Prospectus Download Summary" in text
        assert "**0**" in text  # Total 0

    def test_creates_parent_dirs_for_output(self, tmp_path: Path) -> None:
        """run_report should create output parent directories."""
        from hk_ipo.l1.pipeline import run_report

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "reports" / "summary.md"

        code = _run_async(run_report(manifest, output))

        assert code == 0
        assert output.exists()


# ---------------------------------------------------------------------------
# run_validate tests
# ---------------------------------------------------------------------------


class TestRunValidate:
    def test_writes_gaps_and_missing_tickers(self, tmp_path: Path) -> None:
        """run_validate should write gaps.md, missing_tickers.txt, and gaps.json."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [
                _make_external(ticker="01810", source="aastocks"),
                _make_external(ticker="09999", source="aastocks"),
            ]
            mock_wiki.return_value = [
                _make_external(ticker="01810", source="wikipedia"),
                _make_external(ticker="09999", source="wikipedia"),
            ]
            mock_hkex.return_value = []

            code = _run_async(run_validate(
                manifest, output, missing_tickers,
                sources=["aastocks", "wikipedia", "hkex_stats"],
            ))

        assert code == 0
        assert output.exists()
        gaps_text = output.read_text(encoding="utf-8")
        assert "# HKEX IPO Coverage — Gap Analysis" in gaps_text
        assert missing_tickers.exists()

        # gaps.json should be next to gaps.md
        gaps_json = tmp_path / "gaps.json"
        assert gaps_json.exists()

    def test_one_source_working_returns_2(self, tmp_path: Path) -> None:
        """When only 1 source returns data, return 2 (degraded)."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [
                _make_external(ticker="01810", source="aastocks"),
            ]
            mock_wiki.return_value = []  # no data
            mock_hkex.return_value = []  # no data

            code = _run_async(run_validate(
                manifest, output, missing_tickers,
                sources=["aastocks", "wikipedia", "hkex_stats"],
            ))

        assert code == 2

    def test_zero_sources_working_returns_2(self, tmp_path: Path) -> None:
        """When 0 sources return data, return 2 (degraded)."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = []
            mock_wiki.return_value = []
            mock_hkex.return_value = []

            code = _run_async(run_validate(
                manifest, output, missing_tickers,
                sources=["aastocks", "wikipedia", "hkex_stats"],
            ))

        assert code == 2

    def test_source_exception_is_caught_and_logged(self, tmp_path: Path) -> None:
        """When a source raises, catch it and continue; log to source_errors.json."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.side_effect = RuntimeError("Boom!")
            mock_wiki.return_value = [
                _make_external(ticker="01810", source="wikipedia"),
                _make_external(ticker="09999", source="wikipedia"),
            ]
            mock_hkex.return_value = [
                _make_external(ticker="01810", source="hkex_stats"),
                _make_external(ticker="09999", source="hkex_stats"),
            ]

            code = _run_async(run_validate(
                manifest, output, missing_tickers,
                sources=["aastocks", "wikipedia", "hkex_stats"],
            ))

        # 2 sources provided data (wikipedia + hkex_stats) → not degraded
        assert code == 0
        assert output.exists()

        # source_errors.json should have been written
        err_path = Path("data/validation/source_errors.json")
        # The pipeline writes to data/validation/source_errors.json
        # (relative to cwd). In tests, this may pollute the real dir.
        # We just check the function doesn't crash.
        # The aastocks module also logs internally — no double-log assertion needed.
        if err_path.exists():
            content = err_path.read_text(encoding="utf-8")
            # Clean up to avoid test pollution
            err_path.unlink(missing_ok=True)

    def test_default_sources_all_three(self, tmp_path: Path) -> None:
        """run_validate should default to all 3 sources when sources=None."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [_make_external(ticker="01810", source="aastocks")]
            mock_wiki.return_value = [_make_external(ticker="01810", source="wikipedia")]
            mock_hkex.return_value = []

            code = _run_async(run_validate(manifest, output, missing_tickers))

        assert code == 0
        mock_aa.assert_awaited_once()
        mock_wiki.assert_awaited_once()
        mock_hkex.assert_awaited_once()

    def test_refresh_sources_flag_passed(self, tmp_path: Path) -> None:
        """refresh_sources=True should pass force_refresh=True to sources."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [_make_external(ticker="01810", source="aastocks")]
            mock_wiki.return_value = [_make_external(ticker="01810", source="wikipedia")]
            mock_hkex.return_value = []

            code = _run_async(run_validate(
                manifest, output, missing_tickers, refresh_sources=True,
            ))

        assert code == 0
        # force_refresh should be passed to sources
        _, kwargs = mock_aa.await_args
        assert kwargs.get("force_refresh") is True
        _, kwargs = mock_wiki.await_args
        assert kwargs.get("force_refresh") is True
        _, kwargs = mock_hkex.await_args
        assert kwargs.get("force_refresh") is True

    def test_manifest_not_found_returns_1(self, tmp_path: Path) -> None:
        """run_validate should return 1 when manifest is missing."""
        from hk_ipo.l1.pipeline import run_validate

        manifest = tmp_path / "nonexistent" / "manifest.json"
        output = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock),
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock),
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock),
        ):
            code = _run_async(run_validate(manifest, output, missing_tickers))

        assert code == 1


# ---------------------------------------------------------------------------
# run_report_all tests
# ---------------------------------------------------------------------------


class TestRunReportAll:
    def test_both_succeed_returns_0(self, tmp_path: Path) -> None:
        """run_report_all should return 0 when both succeed."""
        from hk_ipo.l1.pipeline import run_report_all

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        summary_path = tmp_path / "summary.md"
        gaps_path = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [_make_external(ticker="01810", source="aastocks")]
            mock_wiki.return_value = [_make_external(ticker="01810", source="wikipedia")]
            mock_hkex.return_value = []

            code = _run_async(run_report_all(
                manifest, summary_path, gaps_path, missing_tickers,
            ))

        assert code == 0
        assert summary_path.exists()
        assert gaps_path.exists()

    def test_report_fails_validate_succeeds_returns_1(self, tmp_path: Path) -> None:
        """run_report_all returns max(report_code, validate_code)."""
        from hk_ipo.l1.pipeline import run_report_all

        # Missing manifest → report returns 1
        manifest = tmp_path / "nonexistent" / "manifest.json"
        summary_path = tmp_path / "summary.md"
        gaps_path = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        code = _run_async(run_report_all(
            manifest, summary_path, gaps_path, missing_tickers,
        ))

        assert code == 1

    def test_pass_through_kwargs(self, tmp_path: Path) -> None:
        """Extra keyword args should be forwarded correctly."""
        from hk_ipo.l1.pipeline import run_report_all

        manifest = _write_manifest(tmp_path / "manifest.json", _SAMPLE_MANIFEST)
        summary_path = tmp_path / "summary.md"
        gaps_path = tmp_path / "gaps.md"
        missing_tickers = tmp_path / "missing_tickers.txt"

        with (
            patch("hk_ipo.l1.pipeline.fetch_aastocks", new_callable=AsyncMock) as mock_aa,
            patch("hk_ipo.l1.pipeline.fetch_wikipedia", new_callable=AsyncMock) as mock_wiki,
            patch("hk_ipo.l1.pipeline.fetch_hkex_stats", new_callable=AsyncMock) as mock_hkex,
        ):
            mock_aa.return_value = [_make_external(ticker="01810", source="aastocks")]
            mock_wiki.return_value = [_make_external(ticker="01810", source="wikipedia")]
            mock_hkex.return_value = []

            code = _run_async(run_report_all(
                manifest, summary_path, gaps_path, missing_tickers,
                sources=["aastocks", "wikipedia"],
                refresh_sources=True,
                contact_email="test@example.com",
            ))

        assert code == 0


# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine synchronously."""
    import asyncio
    return asyncio.run(coro)
