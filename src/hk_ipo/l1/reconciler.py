"""Gap-analysis reconciler — compares manifest coverage against external sources."""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date
from functools import reduce

from hk_ipo.l1.models import ExternalIPO, GapReport, NormalizedEntry


def reconcile(
    manifest: list[NormalizedEntry],
    sources: dict[str, list[ExternalIPO]],
) -> GapReport:
    """Compare manifest coverage against external source data and produce a GapReport.

    Applies the two-source-agreement rule: a ticker is considered "confirmed"
    only when at least two independent external sources list it as an IPO.
    """
    manifest_success: set[str] = {e.hk_ticker for e in manifest if e.status == "success"}
    manifest_skipped: dict[str, str] = {
        e.hk_ticker: e.status for e in manifest if e.status.startswith("skipped")
    }
    manifest_all: set[str] = manifest_success | set(manifest_skipped)

    # Build per-source ticker sets
    by_source: dict[str, set[str]] = {
        name: {ipo.hk_ticker for ipo in ipos} for name, ipos in sources.items()
    }

    # Count how many external sources claim each ticker
    ticker_source_count: Counter[str] = Counter()
    for tickers in by_source.values():
        for t in tickers:
            ticker_source_count[t] += 1

    # Two-source-agreement rule
    source_count = len(by_source)
    degraded = source_count < 2

    # Only apply two-source rule when not degraded
    if degraded:
        confirmed: set[str] = set()
    else:
        confirmed = {t for t, n in ticker_source_count.items() if n >= 2}

    # Compute gap sets
    missing_from_manifest: set[str] = confirmed - manifest_all
    wrongly_skipped: set[str] = confirmed & set(manifest_skipped)

    # Extra in manifest: we downloaded successfully but no source confirms it
    all_source_tickers: set[str] = reduce(set.union, by_source.values(), set())
    extra_in_manifest: set[str] = manifest_success - all_source_tickers

    # Single-source candidates: mentioned by 1 source, not yet in manifest
    single_source_candidates: set[str] = {
        t for t, n in ticker_source_count.items() if n == 1
    } - manifest_all

    # Compute derived fields
    per_year_counts = _compute_per_year_counts(manifest, sources)
    period = _compute_period(manifest)

    return GapReport(
        period=period,
        manifest_success=manifest_success,
        manifest_skipped=manifest_skipped,
        by_source=by_source,
        missing_from_manifest=missing_from_manifest,
        wrongly_skipped=wrongly_skipped,
        extra_in_manifest=extra_in_manifest,
        single_source_candidates=single_source_candidates,
        per_year_counts=per_year_counts,
        degraded=degraded,
    )


def _compute_period(manifest: list[NormalizedEntry]) -> tuple[date, date]:
    """Derive the covered period from min/max year+month across manifest entries.

    Entries with year=0 are ignored. Returns (today, today) when no valid
    entries exist.
    """
    valid_ym: list[tuple[int, int]] = [
        (e.year, e.month) for e in manifest if e.year > 0
    ]
    if not valid_ym:
        today = date.today()
        return (today, today)

    min_year, min_month = min(valid_ym)
    max_year, max_month = max(valid_ym)

    start = date(min_year, min_month, 1)
    _, last_day = calendar.monthrange(max_year, max_month)
    end = date(max_year, max_month, last_day)

    return (start, end)


def _compute_per_year_counts(
    manifest: list[NormalizedEntry],
    sources: dict[str, list[ExternalIPO]],
) -> dict[int, dict[str, int]]:
    """Build per-year count tables for manifest successes and each source.

    Returns a dict mapping year -> {source_name/``"manifest_success"`` -> count}.
    """
    # Collect all years
    years: set[int] = set()
    for e in manifest:
        if e.year > 0:
            years.add(e.year)
    for ipos in sources.values():
        for ipo in ipos:
            if ipo.list_date is not None:
                years.add(ipo.list_date.year)

    result: dict[int, dict[str, int]] = {}

    for year in sorted(years):
        counts: dict[str, int] = {}

        # Manifest successes for this year
        counts["manifest_success"] = sum(
            1 for e in manifest if e.year == year and e.status == "success"
        )

        # Per-source counts
        for source_name, ipos in sources.items():
            counts[source_name] = sum(
                1
                for ipo in ipos
                if ipo.list_date is not None and ipo.list_date.year == year
            )

        result[year] = counts

    return result
