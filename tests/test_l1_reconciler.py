"""Tests for hk_ipo.l1.reconciler — gap analysis logic."""
from __future__ import annotations

from datetime import date

import pytest

from hk_ipo.l1.models import ExternalIPO, GapReport, NormalizedEntry


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
    doc_url: str | None = "https://example.com/sehk/2024/0301/doc.pdf",
) -> NormalizedEntry:
    return NormalizedEntry(
        hk_ticker=ticker,
        status=status,
        year=year,
        month=month,
        company_name_en=company_name,
        doc_url=doc_url,
        file_path=f"{ticker}.pdf" if status == "success" else None,
    )


def _make_external(
    *,
    ticker: str = "01810",
    company: str = "TestCo Ltd",
    list_date: date | None = date(2024, 3, 1),
    source: str = "aastocks",
    source_url: str = "https://aastocks.com/ipo/01810",
) -> ExternalIPO:
    return ExternalIPO(
        hk_ticker=ticker,
        company_name=company,
        list_date=list_date,
        source=source,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Basic reconciliation
# ---------------------------------------------------------------------------


class TestBasicReconciliation:
    def test_two_sources_agree_missing_from_manifest(self) -> None:
        """2 sources agree on ticker not in manifest -> missing_from_manifest."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810")]
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
            "wikipedia": [_make_external(ticker="09988", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        assert "09988" in report.missing_from_manifest
        assert "09988" not in report.single_source_candidates
        assert "09988" not in report.wrongly_skipped

    def test_single_source_goes_to_candidates_not_missing(self) -> None:
        """1 source only -> single_source_candidates, NOT missing_from_manifest."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810")]
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
        }

        report = reconcile(manifest, sources)

        assert "09988" in report.single_source_candidates
        assert "09988" not in report.missing_from_manifest
        assert report.missing_from_manifest == set()

    def test_ticker_in_manifest_success_not_in_gap_sets(self) -> None:
        """Ticker in manifest as success + confirmed by sources -> no gap."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="success")]
        sources = {
            "aastocks": [_make_external(ticker="01810", source="aastocks")],
            "wikipedia": [_make_external(ticker="01810", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        assert "01810" not in report.missing_from_manifest
        assert "01810" not in report.wrongly_skipped
        assert "01810" in report.manifest_success


# ---------------------------------------------------------------------------
# Wrongly skipped
# ---------------------------------------------------------------------------


class TestWronglySkipped:
    def test_skipped_confirmed_by_two_sources(self) -> None:
        """Ticker skipped in manifest, 2 sources confirm -> wrongly_skipped."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="skipped_no_english")]
        sources = {
            "aastocks": [_make_external(ticker="01810", source="aastocks")],
            "wikipedia": [_make_external(ticker="01810", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        assert "01810" in report.wrongly_skipped
        assert "01810" not in report.missing_from_manifest
        assert report.manifest_skipped == {"01810": "skipped_no_english"}

    def test_skipped_confirmed_by_two_sources_wrong_doc_type(self) -> None:
        """Wrongly skipped should catch all skip statuses."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="skipped_wrong_doc_type")]
        sources = {
            "aastocks": [_make_external(ticker="01810", source="aastocks")],
            "wikipedia": [_make_external(ticker="01810", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        assert "01810" in report.wrongly_skipped


# ---------------------------------------------------------------------------
# Extra in manifest
# ---------------------------------------------------------------------------


class TestExtraInManifest:
    def test_success_but_no_source_confirms(self) -> None:
        """Ticker downloaded successfully but no source confirms -> extra_in_manifest."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="success")]
        sources: dict[str, list[ExternalIPO]] = {}

        report = reconcile(manifest, sources)

        assert "01810" in report.extra_in_manifest

    def test_success_not_confirmed_by_any_source(self) -> None:
        """Ticker in manifest success, 1 source has diff tickers -> extra_in_manifest."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [
            _make_entry(ticker="01810", status="success"),
            _make_entry(ticker="09988", status="success"),
        ]
        sources = {
            "aastocks": [_make_external(ticker="01810", source="aastocks")],
        }

        report = reconcile(manifest, sources)

        assert "09988" in report.extra_in_manifest
        assert "01810" not in report.extra_in_manifest


# ---------------------------------------------------------------------------
# Degraded modes
# ---------------------------------------------------------------------------


class TestDegradedModes:
    def test_zero_sources_all_confirmed_sets_empty(self) -> None:
        """0 sources -> all confirmation sets empty, degraded=True."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="success")]
        sources: dict[str, list[ExternalIPO]] = {}

        report = reconcile(manifest, sources)

        assert report.missing_from_manifest == set()
        assert report.wrongly_skipped == set()
        assert report.single_source_candidates == set()
        assert report.degraded is True

    def test_one_source_all_confirmed_sets_empty(self) -> None:
        """1 source -> two-source rule can't fire, missing/wrongly empty, degraded=True."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810")]
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
        }

        report = reconcile(manifest, sources)

        assert report.missing_from_manifest == set()
        assert report.wrongly_skipped == set()
        assert report.degraded is True
        # Single-source still goes to candidates
        assert "09988" in report.single_source_candidates

    def test_two_sources_not_degraded(self) -> None:
        """2 sources -> degraded=False (two-source rule can fire)."""
        from hk_ipo.l1.reconciler import reconcile

        manifest: list[NormalizedEntry] = []
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
            "wikipedia": [_make_external(ticker="09988", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        assert report.degraded is False


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_empty_manifest_and_sources(self) -> None:
        """Empty manifest + empty sources -> empty GapReport, no crash."""
        from hk_ipo.l1.reconciler import reconcile

        report = reconcile([], {})

        assert report.manifest_success == set()
        assert report.manifest_skipped == {}
        assert report.by_source == {}
        assert report.missing_from_manifest == set()
        assert report.wrongly_skipped == set()
        assert report.extra_in_manifest == set()
        assert report.single_source_candidates == set()
        assert report.per_year_counts == {}

    def test_empty_manifest_with_sources(self) -> None:
        """Empty manifest, 3 sources agree -> missing_from_manifest."""
        from hk_ipo.l1.reconciler import reconcile

        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
            "wikipedia": [_make_external(ticker="09988", source="wikipedia")],
            "hkex_stats": [_make_external(ticker="09988", source="hkex_stats")],
        }

        report = reconcile([], sources)

        assert "09988" in report.missing_from_manifest
        assert len(report.by_source) == 3


# ---------------------------------------------------------------------------
# per_year_counts
# ---------------------------------------------------------------------------


class TestPerYearCounts:
    def test_per_year_counts_computation(self) -> None:
        """per_year_counts should aggregate counts from manifest and sources."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [
            _make_entry(ticker="01810", year=2024, month=6, status="success"),
            _make_entry(ticker="09988", year=2024, month=6, status="skipped_wrong_doc_type"),
            _make_entry(ticker="00001", year=2023, month=1, status="success"),
        ]
        sources = {
            "aastocks": [
                _make_external(ticker="01810", source="aastocks", list_date=date(2024, 6, 1)),
                _make_external(ticker="09988", source="aastocks", list_date=date(2024, 6, 1)),
                _make_external(ticker="00700", source="aastocks", list_date=date(2023, 1, 1)),
            ],
        }

        report = reconcile(manifest, sources)

        assert 2024 in report.per_year_counts
        assert 2023 in report.per_year_counts

        y2024 = report.per_year_counts[2024]
        assert y2024["manifest_success"] == 1
        assert y2024["aastocks"] == 2

        y2023 = report.per_year_counts[2023]
        assert y2023["manifest_success"] == 1
        assert y2023["aastocks"] == 1

    def test_per_year_counts_includes_all_source_names(self) -> None:
        """Each source name should appear as a key in the per-year dict."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", year=2024, month=3, status="success")]
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks", list_date=date(2024, 6, 1))],
            "wikipedia": [_make_external(ticker="01810", source="wikipedia", list_date=date(2024, 3, 1))],
        }

        report = reconcile(manifest, sources)

        y2024 = report.per_year_counts[2024]
        assert "aastocks" in y2024
        assert "wikipedia" in y2024
        assert y2024["aastocks"] == 1
        assert y2024["wikipedia"] == 1

    def test_per_year_counts_handles_null_list_date(self) -> None:
        """ExternalIPOs with list_date=None should not be counted in any year."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", year=2024, month=6, status="success")]
        sources = {
            "aastocks": [
                _make_external(ticker="01810", source="aastocks", list_date=None),
                _make_external(ticker="09988", source="aastocks", list_date=date(2024, 6, 1)),
            ],
        }

        report = reconcile(manifest, sources)

        y2024 = report.per_year_counts[2024]
        # Only 09988 has a list_date in 2024, 01810 has None
        assert y2024["aastocks"] == 1


# ---------------------------------------------------------------------------
# Period computation
# ---------------------------------------------------------------------------


class TestPeriodComputation:
    def test_period_from_manifest_entries(self) -> None:
        """Period should span min/max year+month from manifest entries."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [
            _make_entry(ticker="01810", year=2023, month=12),
            _make_entry(ticker="09988", year=2024, month=3),
        ]

        report = reconcile(manifest, {})

        assert report.period[0] == date(2023, 12, 1)
        assert report.period[1] == date(2024, 3, 31)

    def test_period_empty_manifest(self) -> None:
        """Empty manifest -> period is today/today."""
        from hk_ipo.l1.reconciler import reconcile

        report = reconcile([], {})

        today = date.today()
        assert report.period[0] == today
        assert report.period[1] == today

    def test_period_zero_year_entries_ignored(self) -> None:
        """Entries with year=0 should be ignored for period computation."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [
            _make_entry(ticker="01810", year=2024, month=3),
            _make_entry(ticker="09999", year=0, month=0, doc_url=None),
        ]

        report = reconcile(manifest, {})

        assert report.period[0] == date(2024, 3, 1)
        assert report.period[1] == date(2024, 3, 31)

    def test_period_single_entry(self) -> None:
        """Single entry: start and end are in the same month."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", year=2024, month=2)]

        report = reconcile(manifest, {})

        assert report.period[0] == date(2024, 2, 1)
        # 2024 is leap year, Feb has 29 days
        assert report.period[1] == date(2024, 2, 29)

    def test_period_february_non_leap(self) -> None:
        """February in a non-leap year should end on the 28th."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", year=2023, month=2)]

        report = reconcile(manifest, {})

        assert report.period[1] == date(2023, 2, 28)


# ---------------------------------------------------------------------------
# Multiple sources, edge cases
# ---------------------------------------------------------------------------


class TestMultipleSources:
    def test_three_sources_two_agree_triggers_confirmation(self) -> None:
        """3 sources, 2 agree -> confirmed (missing if not in manifest)."""
        from hk_ipo.l1.reconciler import reconcile

        manifest: list[NormalizedEntry] = []
        sources = {
            "aastocks": [_make_external(ticker="09988", source="aastocks")],
            "wikipedia": [_make_external(ticker="09988", source="wikipedia")],
            "hkex_stats": [_make_external(ticker="00700", source="hkex_stats")],
        }

        report = reconcile(manifest, sources)

        # 09988 confirmed by 2 sources -> missing
        assert "09988" in report.missing_from_manifest
        # 00700 only by 1 source -> candidate
        assert "00700" in report.single_source_candidates

    def test_by_source_preserves_source_names(self) -> None:
        """by_source should contain the source names and their ticker sets."""
        from hk_ipo.l1.reconciler import reconcile

        manifest: list[NormalizedEntry] = []
        sources = {
            "aastocks": [
                _make_external(ticker="09988", source="aastocks"),
                _make_external(ticker="00700", source="aastocks"),
            ],
        }

        report = reconcile(manifest, sources)

        assert report.by_source == {"aastocks": {"09988", "00700"}}

    def test_manifest_status_failed_not_counted(self) -> None:
        """Entries with status 'failed' are neither success nor skipped."""
        from hk_ipo.l1.reconciler import reconcile

        manifest = [_make_entry(ticker="01810", status="failed")]
        sources = {
            "aastocks": [_make_external(ticker="01810", source="aastocks")],
            "wikipedia": [_make_external(ticker="01810", source="wikipedia")],
        }

        report = reconcile(manifest, sources)

        # Failed entry is not in manifest_success or manifest_skipped
        # So it appears as missing_from_manifest (confirmed but not in manifest_all)
        assert "01810" not in report.manifest_success
        assert "01810" not in report.manifest_skipped
        assert "01810" in report.missing_from_manifest

    def test_duplicate_ipo_within_same_source(self) -> None:
        """Duplicate tickers within a single source should be deduplicated."""
        from hk_ipo.l1.reconciler import reconcile

        manifest: list[NormalizedEntry] = []
        sources = {
            "aastocks": [
                _make_external(ticker="09988", source="aastocks"),
                _make_external(ticker="09988", source="aastocks"),
            ],
        }

        report = reconcile(manifest, sources)

        # by_source should deduplicate
        assert report.by_source == {"aastocks": {"09988"}}
