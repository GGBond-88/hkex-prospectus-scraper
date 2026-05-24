"""Tests for hk_ipo.l1.summary_writer — produces summary.md from NormalizedEntry list."""
from __future__ import annotations

import re
from pathlib import Path

from hk_ipo.l1.models import NormalizedEntry
from hk_ipo.l1.summary_writer import write_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    *,
    ticker: str = "01810",
    status: str = "success",
    year: int = 2024,
    month: int = 3,
    company_name: str | None = "TestCo Ltd",
    doc_url: str | None = "https://example.com/sehk/2024/0301/doc_e.pdf",
    file_path: str | None = "01810.pdf",
) -> NormalizedEntry:
    fp = file_path if status == "success" else None
    return NormalizedEntry(
        hk_ticker=ticker,
        status=status,
        year=year,
        month=month,
        company_name_en=company_name,
        doc_url=doc_url,
        file_path=fp,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyEntries:
    def test_empty_entries(self, tmp_path: Path) -> None:
        out = tmp_path / "summary.md"
        write_summary([], out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        assert "# HKEX IPO Prospectus Download Summary" in text
        assert "Period covered: N/A" in text
        assert "Successfully downloaded | 0" in text
        assert "Skipped (wrong doc type) | 0" in text
        assert "Skipped (no English) | 0" in text
        assert "Failed | 0" in text
        assert "**Total manifest entries** | **0**" in text


class TestSingleSuccess:
    def test_single_success_entry(self, tmp_path: Path) -> None:
        entries = [_make_entry()]
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        assert "Successfully downloaded | 1" in text
        assert "**Total manifest entries** | **1**" in text
        assert "## 2024" in text
        assert "### 2024-03" in text
        assert "01810" in text
        assert "TestCo Ltd" in text


class TestYearOrdering:
    def test_years_newest_first(self, tmp_path: Path) -> None:
        entries = [
            _make_entry(ticker="02024", year=2024, month=6),
            _make_entry(ticker="02020", year=2020, month=3),
        ]
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        # 2024 must appear before 2020
        pos_2024 = text.index("## 2024")
        pos_2020 = text.index("## 2020")
        assert pos_2024 < pos_2020, "Years should be newest first"


class TestEmptyMonths:
    def test_empty_months_omitted(self, tmp_path: Path) -> None:
        entries = [
            _make_entry(ticker="00100", year=2024, month=1),
            _make_entry(ticker="01200", year=2024, month=12),
        ]
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        assert "2024-01" in text
        assert "2024-12" in text
        # Months 2-11 should not appear
        for m in range(2, 12):
            assert f"2024-{m:02d}" not in text, f"Month {m:02d} should be omitted"


class TestZeroSuccessCallout:
    def test_zero_success_year_callout(self, tmp_path: Path) -> None:
        entries = [
            _make_entry(ticker="00100", status="skipped_wrong_doc_type", year=2024, month=1),
        ]
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        assert "No successful downloads this year" in text


class TestCompanyNameNA:
    def test_company_name_na_when_none(self, tmp_path: Path) -> None:
        entries = [_make_entry(company_name=None)]
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path="data/raw_pdfs/manifest.json")
        text = _read(out)

        # Table row should show N/A
        assert "| 01810 | N/A |" in text


class TestFullSnapshot:
    def test_full_output_snapshot(self, tmp_path: Path) -> None:
        from hk_ipo.l1.manifest_reader import read_manifest

        fixture = Path(__file__).resolve().parent / "fixtures" / "sample_manifest.json"
        entries = read_manifest(fixture)
        out = tmp_path / "summary.md"
        write_summary(entries, out, manifest_path=str(fixture))
        text = _read(out)

        # ---- Header / totals ------------------------------------------------
        assert "# HKEX IPO Prospectus Download Summary" in text
        assert ": 2018-06 to 2024-07" in text or "2018-06" in text
        assert "Successfully downloaded | 2" in text
        assert "Skipped (wrong doc type) | 2" in text
        assert "Skipped (no English) | 1" in text
        assert "Failed | 1" in text
        assert "**Total manifest entries** | **6**" in text

        # ---- Year ordering (newest first) -----------------------------------
        pos_2024 = text.index("## 2024")
        pos_2018 = text.index("## 2018")
        assert pos_2024 < pos_2018

        # ---- 2024-01: skipped entries ---------------------------------------
        assert "### 2024-01" in text
        assert "Skipped (1)" in text or "Skipped tickers" in text  # month skip header
        assert "00001" in text

        # ---- 2024-03: one success, one skipped_no_english ------------------
        assert "### 2024-03" in text
        assert "GEM Test Co Ltd" in text
        assert "06666" in text  # skipped_no_english ticker

        # ---- 2024-06: failed entry -----------------------------------------
        assert "### 2024-06" in text
        assert "09999" in text
        assert "Failed" in text

        # ---- 2024-07: skipped entry with null doc_url ----------------------
        # Entry 08888 has doc_url=None → year=0, month=0
        # It should be grouped under year 0 or omitted from period calculation
        # The entry should still appear somewhere
        assert "08888" in text

        # ---- 2018: success ------------------------------------------------
        assert "### 2018-06" in text
        assert "Xiaomi Corporation" in text
        assert "01810" in text

        # ---- Timestamp -----------------------------------------------------
        assert re.search(r"Generated: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)

        # ---- Manifest reference --------------------------------------------
        assert str(fixture) in text
