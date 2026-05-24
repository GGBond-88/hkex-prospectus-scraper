"""Writes gap-analysis reports: gaps.md, missing_tickers.txt, and gaps.json."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from hk_ipo.l1.models import GapReport


def write_gaps(report: GapReport, output_path: Path) -> None:
    """Write the human-readable gap analysis to *output_path* as Markdown.

    The file is always fully overwritten. Parent directories are created as
    needed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    # ---- Header -----------------------------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    period_start = report.period[0].isoformat()
    period_end = report.period[1].isoformat()
    source_names = ", ".join(sorted(report.by_source))

    lines.append("# HKEX IPO Coverage — Gap Analysis")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append(f"Period covered: {period_start} to {period_end}")
    lines.append(f"Sources consulted: {source_names}" if source_names else "Sources consulted: (none)")
    lines.append("")

    # ---- Degraded banner --------------------------------------------------
    if report.degraded:
        lines.append("> ⚠ **Degraded mode** — fewer than 2 sources provided data.")
        lines.append("> The two-source-agreement rule is disabled. All gap findings are empty/limited.")
        lines.append("")

    # ---- Source agreement by year -----------------------------------------
    lines.append("## Source agreement by year")
    lines.append("")
    _write_year_table(report, lines)

    # ---- Missing from manifest --------------------------------------------
    lines.append("## Missing from manifest — confirmed by >=2 sources")
    lines.append("")
    _write_missing_table(report, lines)

    # ---- Wrongly skipped --------------------------------------------------
    lines.append("## Wrongly skipped — manifest says skipped, >=2 sources say IPO")
    lines.append("")
    _write_wrongly_skipped_table(report, lines)

    # ---- Single-source candidates -----------------------------------------
    lines.append("## Single-source candidates (triage manually)")
    lines.append("")
    _write_single_source_table(report, lines)

    # ---- Extra in manifest ------------------------------------------------
    lines.append("## Extra in manifest — we downloaded, no source confirms")
    lines.append("")
    if report.extra_in_manifest:
        lines.append("| Ticker |")
        lines.append("|---|")
        for ticker in sorted(report.extra_in_manifest):
            lines.append(f"| {ticker} |")
        lines.append("")
    else:
        lines.append("No extra entries found.")
        lines.append("")

    # ---- Source errors ----------------------------------------------------
    lines.append("## Source errors")
    lines.append("")
    lines.append("(see data/validation/source_errors.json for details)")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_missing_tickers(report: GapReport, output_path: Path) -> None:
    """Write the union of missing_from_manifest + wrongly_skipped as a
    newline-delimited, sorted list of 5-digit padded tickers.

    Parent directories are created as needed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_missing = sorted(report.missing_from_manifest | report.wrongly_skipped)

    if all_missing:
        output_path.write_text("\n".join(all_missing) + "\n", encoding="utf-8")
    else:
        output_path.write_text("", encoding="utf-8")


def write_gaps_json(report: GapReport, output_path: Path) -> None:
    """Write a machine-readable JSON representation of the GapReport.

    Sets are serialized as sorted lists. Parent directories are created.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, object] = {
        "period": [report.period[0].isoformat(), report.period[1].isoformat()],
        "degraded": report.degraded,
        "manifest_success": sorted(report.manifest_success),
        "manifest_skipped": dict(sorted(report.manifest_skipped.items())),
        "by_source": {
            name: sorted(tickers)
            for name, tickers in sorted(report.by_source.items())
        },
        "missing_from_manifest": sorted(report.missing_from_manifest),
        "wrongly_skipped": sorted(report.wrongly_skipped),
        "extra_in_manifest": sorted(report.extra_in_manifest),
        "single_source_candidates": sorted(report.single_source_candidates),
        "per_year_counts": {
            str(year): counts for year, counts in sorted(report.per_year_counts.items())
        },
    }

    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _status_label(manifest_count: int, median_external: int) -> str:
    """Classify coverage status based on manifest vs external source counts.

    Returns an emoji-labelled status string or "—" when no external data.
    """
    if median_external == 0:
        return "—"  # em dash
    pct = manifest_count / median_external * 100
    if pct >= 80:
        return "✅ OK"  # ✅ OK
    if pct >= 50:
        return "⚠ Minor gap"  # ⚠ Minor gap
    return "❌ Large gap"  # ❌ Large gap


# ---------------------------------------------------------------------------
# Internal table builders
# ---------------------------------------------------------------------------


def _write_year_table(report: GapReport, lines: list[str]) -> None:
    if not report.per_year_counts:
        lines.append("No year data available.")
        lines.append("")
        return

    source_names = sorted(report.by_source)

    # Build header
    header_cols = ["Year", "Manifest (success)"] + source_names + ["Status"]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("|" + "|".join("---" for _ in header_cols) + "|")

    for year in sorted(report.per_year_counts):
        counts = report.per_year_counts[year]
        manifest_count = counts.get("manifest_success", 0)

        # Median of external source counts (exclude manifest_success)
        external_counts = [
            counts.get(src, 0) for src in source_names
        ]
        median_external = int(statistics.median(external_counts)) if external_counts else 0

        cols = [str(year), str(manifest_count)]
        cols.extend(str(counts.get(src, 0)) for src in source_names)
        cols.append(_status_label(manifest_count, median_external))

        lines.append("| " + " | ".join(cols) + " |")

    lines.append("")


def _write_missing_table(report: GapReport, lines: list[str]) -> None:
    missing = sorted(report.missing_from_manifest)
    if not missing:
        lines.append("No tickers missing from manifest.")
        lines.append("")
        return

    lines.append("| Ticker | Confirmed by |")
    lines.append("|---|---|")
    for ticker in missing:
        confirmers = _confirming_sources(ticker, report)
        lines.append(f"| {ticker} | {confirmers} |")

    total = len(report.missing_from_manifest) + len(report.wrongly_skipped)
    lines.append("")
    lines.append(f"({total} tickers requiring action — written to missing_tickers.txt)")
    lines.append("")


def _write_wrongly_skipped_table(report: GapReport, lines: list[str]) -> None:
    skipped = sorted(report.wrongly_skipped)
    if not skipped:
        lines.append("No wrongly skipped tickers.")
        lines.append("")
        return

    lines.append("| Ticker | Manifest status | Confirmed by |")
    lines.append("|---|---|---|")
    for ticker in skipped:
        status = report.manifest_skipped.get(ticker, "unknown")
        confirmers = _confirming_sources(ticker, report)
        lines.append(f"| {ticker} | {status} | {confirmers} |")
    lines.append("")


def _write_single_source_table(report: GapReport, lines: list[str]) -> None:
    candidates = sorted(report.single_source_candidates)
    if not candidates:
        lines.append("No single-source candidates.")
        lines.append("")
        return

    lines.append("| Ticker | Source |")
    lines.append("|---|---|")
    for ticker in candidates:
        src = _single_source_for(ticker, report)
        lines.append(f"| {ticker} | {src} |")
    lines.append("")


def _confirming_sources(ticker: str, report: GapReport) -> str:
    """Return a comma-separated list of source names that claim this ticker."""
    sources = sorted(
        name for name, tickers in report.by_source.items() if ticker in tickers
    )
    return ", ".join(sources) if sources else "unknown"


def _single_source_for(ticker: str, report: GapReport) -> str:
    """Return the single source name that claims this ticker."""
    for name, tickers in report.by_source.items():
        if ticker in tickers:
            return name
    return "unknown"
