"""Produces ./summary.md from a list of NormalizedEntry."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hk_ipo.l1.models import NormalizedEntry

_SKIP_STATUSES = frozenset({"skipped_wrong_doc_type", "skipped_no_english"})


def write_summary(entries: list[NormalizedEntry], output_path: Path) -> None:
    """Write a Markdown summary of manifest entries to *output_path*.

    The file is always fully overwritten.
    """
    # ---- totals ------------------------------------------------------------
    total = len(entries)
    n_success = sum(1 for e in entries if e.status == "success")
    n_skipped_wrong = sum(1 for e in entries if e.status == "skipped_wrong_doc_type")
    n_skipped_no_en = sum(1 for e in entries if e.status == "skipped_no_english")
    n_failed = sum(1 for e in entries if e.status == "failed")

    # ---- period covered ----------------------------------------------------
    valid_ym = [(e.year, e.month) for e in entries if e.year > 0]
    if valid_ym:
        min_ym = min(valid_ym)
        max_ym = max(valid_ym)
        period = f"{min_ym[0]}-{min_ym[1]:02d} to {max_ym[0]}-{max_ym[1]:02d}"
    else:
        period = "N/A"

    # ---- group by (year, month) --------------------------------------------
    groups: dict[tuple[int, int], list[NormalizedEntry]] = defaultdict(list)
    for e in entries:
        groups[(e.year, e.month)].append(e)

    # ---- build markdown ----------------------------------------------------
    lines: list[str] = []

    # Header
    lines.append("# HKEX IPO Prospectus Download Summary")
    lines.append("")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"Generated: {ts}")
    lines.append(f"Period covered: {period}")
    lines.append("Manifest: data/raw_pdfs/manifest.json")
    lines.append("")

    # Totals table
    lines.append("## Totals")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    lines.append(f"| Successfully downloaded | {n_success} |")
    lines.append(f"| Skipped (wrong doc type) | {n_skipped_wrong} |")
    lines.append(f"| Skipped (no English) | {n_skipped_no_en} |")
    lines.append(f"| Failed | {n_failed} |")
    lines.append(f"| **Total manifest entries** | **{total}** |")
    lines.append("")

    # Per-year sections (newest first)
    year_to_months: dict[int, dict[int, list[NormalizedEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for (y, m), month_entries in groups.items():
        year_to_months[y][m] = month_entries

    for year in sorted(year_to_months, reverse=True):
        months = year_to_months[year]
        year_entries = [e for m_entries in months.values() for e in m_entries]
        y_success = sum(1 for e in year_entries if e.status == "success")
        y_skipped = sum(1 for e in year_entries if e.status in _SKIP_STATUSES)
        y_failed = sum(1 for e in year_entries if e.status == "failed")

        lines.append(f"## {year}")
        lines.append("")
        lines.append(
            f"**Downloaded: {y_success} / Skipped: {y_skipped} / Failed: {y_failed}**"
        )
        lines.append("")

        if y_success == 0 and year_entries:
            lines.append(
                "> ⚠ No successful downloads this year. See gaps.md for diagnostic."
            )
            lines.append("")

        # Months in ascending order within the year
        for month in sorted(months):
            month_entries = months[month]
            successes = [e for e in month_entries if e.status == "success"]
            skipped = [e for e in month_entries if e.status in _SKIP_STATUSES]
            failed = [e for e in month_entries if e.status == "failed"]

            # Successes
            if successes:
                lines.append(f"### {year}-{month:02d}")
                tickers_str = ", ".join(e.hk_ticker for e in successes)
                lines.append(f"Downloaded ({len(successes)}): {tickers_str}")
                lines.append("")
                lines.append("| Ticker | Company |")
                lines.append("|---|---|")
                for e in successes:
                    name = e.company_name_en or "N/A"
                    lines.append(f"| {e.hk_ticker} | {name} |")
                lines.append("")

            # Skipped (group all skip statuses)
            if skipped:
                lines.append(f"### {year}-{month:02d} — Skipped ({len(skipped)})")
                tickers_str = ", ".join(e.hk_ticker for e in skipped)
                lines.append(f"Skipped tickers: {tickers_str}")
                lines.append("")

            # Failed
            if failed:
                lines.append(f"### {year}-{month:02d} — Failed ({len(failed)})")
                lines.append("| Ticker | Error |")
                lines.append("|---|---|")
                for e in failed:
                    error = e.doc_url or "N/A"
                    lines.append(f"| {e.hk_ticker} | {error} |")
                lines.append("")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
