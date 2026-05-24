"""Tests for hk_ipo.l1.gaps_writer — gap report output."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from hk_ipo.l1.models import ExternalIPO, GapReport, NormalizedEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(**overrides: object) -> GapReport:
    """Build a GapReport with sensible defaults, overridable per-test."""
    kwargs: dict[str, object] = {
        "period": (date(2024, 1, 1), date(2024, 12, 31)),
        "manifest_success": set(),
        "manifest_skipped": {},
        "by_source": {"aastocks": set(), "wikipedia": set()},
        "missing_from_manifest": set(),
        "wrongly_skipped": set(),
        "extra_in_manifest": set(),
        "single_source_candidates": set(),
        "per_year_counts": {},
        "degraded": False,
    }
    kwargs.update(overrides)  # type: ignore[typeddict-item]
    return GapReport(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# write_gaps — Markdown output
# ---------------------------------------------------------------------------


class TestWriteGaps:
    def test_creates_output_file(self, tmp_path: Path) -> None:
        """write_gaps should create the output markdown file."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report()
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "# HKEX IPO Coverage — Gap Analysis" in text

    def test_header_includes_generated_timestamp(self, tmp_path: Path) -> None:
        """Generated line should contain an ISO timestamp."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report()
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "Generated: " in text
        # Should have ISO-8601-like timestamp
        import re

        assert re.search(r"Generated: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)

    def test_header_includes_period(self, tmp_path: Path) -> None:
        """The period covered should appear in the header."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(period=(date(2023, 6, 1), date(2024, 3, 31)))
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "Period covered: 2023-06-01 to 2024-03-31" in text

    def test_header_includes_sources_consulted(self, tmp_path: Path) -> None:
        """The sources consulted line should list source names."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(by_source={"aastocks": set(), "wikipedia": set()})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "Sources consulted: aastocks, wikipedia" in text

    def test_degraded_banner_when_degraded(self, tmp_path: Path) -> None:
        """Degraded report should show a warning banner."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(
            degraded=True,
            by_source={"aastocks": set()},
        )
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "Degraded" in text or "degraded" in text or "⚠" in text or "confidence" in text.lower()

    def test_missing_from_manifest_section(self, tmp_path: Path) -> None:
        """Missing tickers section should list confirmed-missing tickers."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(missing_from_manifest={"09988", "00700"})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "## Missing from manifest" in text
        # Both tickers should appear
        assert "09988" in text
        assert "00700" in text

    def test_wrongly_skipped_section(self, tmp_path: Path) -> None:
        """Wrongly skipped tickers should appear in their own section."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(
            wrongly_skipped={"01810"},
            manifest_skipped={"01810": "skipped_no_english"},
        )
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "## Wrongly skipped" in text
        assert "01810" in text
        assert "skipped_no_english" in text

    def test_single_source_candidates_section(self, tmp_path: Path) -> None:
        """Single-source candidates should have their own section."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(single_source_candidates={"09988"})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "## Single-source candidates" in text
        assert "09988" in text

    def test_extra_in_manifest_section(self, tmp_path: Path) -> None:
        """Extra in manifest section should list unconfirmed successes."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(extra_in_manifest={"01810"})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "## Extra in manifest" in text
        assert "01810" in text

    def test_missing_tickers_count_reference(self, tmp_path: Path) -> None:
        """Missing section should reference missing_tickers.txt count."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(missing_from_manifest={"09988", "00700"}, wrongly_skipped={"01810"})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        # Should mention the count or the file name
        assert "missing_tickers.txt" in text

    def test_source_agreement_by_year_table(self, tmp_path: Path) -> None:
        """The per-year table should show counts for each year."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(
            per_year_counts={
                2024: {"manifest_success": 9, "aastocks": 73, "wikipedia": 65},
            },
            by_source={"aastocks": set(), "wikipedia": set()},
        )
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "## Source agreement by year" in text
        assert "2024" in text
        assert "73" in text
        assert "65" in text
        assert "9" in text


# ---------------------------------------------------------------------------
# write_missing_tickers
# ---------------------------------------------------------------------------


class TestWriteMissingTickers:
    def test_writes_union_of_missing_and_wrongly_skipped(self, tmp_path: Path) -> None:
        """Union of missing_from_manifest + wrongly_skipped, sorted."""
        from hk_ipo.l1.gaps_writer import write_missing_tickers

        report = _make_report(
            missing_from_manifest={"09988", "00700"},
            wrongly_skipped={"01810"},
        )
        out = tmp_path / "missing_tickers.txt"
        write_missing_tickers(report, out)

        text = out.read_text(encoding="utf-8").strip()
        lines = text.split("\n")
        assert lines == ["00700", "01810", "09988"]  # sorted 5-digit padded

    def test_only_wrongly_skipped(self, tmp_path: Path) -> None:
        """Only wrongly_skipped entries."""
        from hk_ipo.l1.gaps_writer import write_missing_tickers

        report = _make_report(
            wrongly_skipped={"01810", "00700"},
        )
        out = tmp_path / "missing_tickers.txt"
        write_missing_tickers(report, out)

        text = out.read_text(encoding="utf-8").strip()
        lines = text.split("\n")
        assert lines == ["00700", "01810"]

    def test_empty_produces_empty_file(self, tmp_path: Path) -> None:
        """Empty missing + empty wrongly_skipped -> empty file (or newline only)."""
        from hk_ipo.l1.gaps_writer import write_missing_tickers

        report = _make_report()
        out = tmp_path / "missing_tickers.txt"
        write_missing_tickers(report, out)

        text = out.read_text(encoding="utf-8").strip()
        assert text == ""

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """write_missing_tickers should create parent directories."""
        from hk_ipo.l1.gaps_writer import write_missing_tickers

        report = _make_report(missing_from_manifest={"09988"})
        deep = tmp_path / "a" / "b" / "missing_tickers.txt"
        write_missing_tickers(report, deep)

        assert deep.exists()
        assert deep.read_text(encoding="utf-8").strip() == "09988"


# ---------------------------------------------------------------------------
# write_gaps_json
# ---------------------------------------------------------------------------


class TestWriteGapsJson:
    def test_creates_json_file(self, tmp_path: Path) -> None:
        """write_gaps_json should create a machine-readable JSON file."""
        from hk_ipo.l1.gaps_writer import write_gaps_json

        report = _make_report(
            period=(date(2024, 1, 1), date(2024, 6, 30)),
            manifest_success={"01810"},
            missing_from_manifest={"09988"},
            by_source={"aastocks": {"01810", "09988"}},
            per_year_counts={2024: {"manifest_success": 1, "aastocks": 2}},
        )
        out = tmp_path / "gaps.json"
        write_gaps_json(report, out)

        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "period" in data
        assert data["period"] == ["2024-01-01", "2024-06-30"]
        assert "01810" in data["manifest_success"]
        assert "09988" in data["missing_from_manifest"]
        assert data["degraded"] is False

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """write_gaps_json should create parent directories."""
        from hk_ipo.l1.gaps_writer import write_gaps_json

        report = _make_report()
        deep = tmp_path / "x" / "y" / "z" / "gaps.json"
        write_gaps_json(report, deep)

        assert deep.exists()

    def test_sets_are_serialized_as_sorted_lists(self, tmp_path: Path) -> None:
        """Sets should become sorted JSON lists."""
        from hk_ipo.l1.gaps_writer import write_gaps_json

        report = _make_report(
            missing_from_manifest={"09988", "00700", "01810"},
            by_source={"aastocks": {"00700"}, "wikipedia": {"01810"}},
        )
        out = tmp_path / "gaps.json"
        write_gaps_json(report, out)

        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["missing_from_manifest"] == ["00700", "01810", "09988"]
        assert data["by_source"] == {
            "aastocks": ["00700"],
            "wikipedia": ["01810"],
        }


# ---------------------------------------------------------------------------
# Status label
# ---------------------------------------------------------------------------


class TestStatusLabel:
    def test_empty_external(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(5, 0) == "—"

    def test_ok(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(80, 100) == "✅ OK"

    def test_ok_boundary(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        # 16/20 = 80% -> OK
        assert _status_label(16, 20) == "✅ OK"

    def test_minor_gap(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(60, 100) == "⚠ Minor gap"

    def test_minor_gap_boundary(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(10, 20) == "⚠ Minor gap"  # 50%

    def test_large_gap(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(40, 100) == "❌ Large gap"

    def test_large_gap_boundary(self) -> None:
        from hk_ipo.l1.gaps_writer import _status_label

        assert _status_label(9, 20) == "❌ Large gap"  # 45%


# ---------------------------------------------------------------------------
# Empty report
# ---------------------------------------------------------------------------


class TestEmptyReport:
    def test_empty_report_still_writes_valid_markdown(self, tmp_path: Path) -> None:
        """An empty GapReport should still produce valid markdown output."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(by_source={})
        out = tmp_path / "gaps.md"
        write_gaps(report, out)

        text = out.read_text(encoding="utf-8")
        assert "# HKEX IPO Coverage — Gap Analysis" in text
        assert "## Missing from manifest" in text
        assert "## Wrongly skipped" in text
        assert "## Single-source candidates" in text
        assert "## Extra in manifest" in text

    def test_empty_report_no_crash(self, tmp_path: Path) -> None:
        """Writing an entirely empty GapReport should not raise."""
        from hk_ipo.l1.gaps_writer import write_gaps

        report = _make_report(period=(date.today(), date.today()), by_source={})
        out = tmp_path / "gaps.md"
        # Should not raise
        write_gaps(report, out)
        assert out.exists()
