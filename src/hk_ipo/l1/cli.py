"""Argparse subcommand handlers for L1 report / validate / report-all."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_l1_subparsers(subparsers) -> None:
    """Add report, validate, report-all subcommands to an existing subparsers group."""

    # -- report --------------------------------------------------------------
    report_p = subparsers.add_parser(
        "report",
        help="Generate IPO summary report from manifest.",
    )
    report_p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest.json (default: from HK_IPO_DATA_DIR).",
    )
    report_p.add_argument(
        "--output",
        type=Path,
        default=Path("./summary.md"),
        help="Output path for summary report (default: ./summary.md).",
    )
    report_p.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Filter entries on or after this date (inclusive).",
    )
    report_p.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Filter entries on or before this date (inclusive).",
    )

    # -- validate ------------------------------------------------------------
    validate_p = subparsers.add_parser(
        "validate",
        help="Validate manifest coverage against external IPO sources.",
    )
    validate_p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest.json (default: from HK_IPO_DATA_DIR).",
    )
    validate_p.add_argument(
        "--output",
        type=Path,
        default=Path("./gaps.md"),
        help="Output path for gap analysis report (default: ./gaps.md).",
    )
    validate_p.add_argument(
        "--missing-tickers-out",
        type=Path,
        default=Path("data/validation/reconciled/missing_tickers.txt"),
        help="Output path for missing tickers list.",
    )
    validate_p.add_argument(
        "--sources",
        type=str,
        default="hkex_stats,aastocks,wikipedia",
        help="Comma-separated list of sources (default: hkex_stats,aastocks,wikipedia).",
    )
    validate_p.add_argument(
        "--refresh-sources",
        action="store_true",
        help="Bypass source data cache and re-fetch.",
    )

    # -- report-all ----------------------------------------------------------
    report_all_p = subparsers.add_parser(
        "report-all",
        help="Run report and validate in sequence.",
    )
    report_all_p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest.json (default: from HK_IPO_DATA_DIR).",
    )
    report_all_p.add_argument(
        "--summary",
        type=Path,
        default=Path("./summary.md"),
        help="Output path for summary report (default: ./summary.md).",
    )
    report_all_p.add_argument(
        "--gaps",
        type=Path,
        default=Path("./gaps.md"),
        help="Output path for gap analysis report (default: ./gaps.md).",
    )
    report_all_p.add_argument(
        "--missing-tickers-out",
        type=Path,
        default=Path("data/validation/reconciled/missing_tickers.txt"),
        help="Output path for missing tickers list.",
    )
    report_all_p.add_argument(
        "--sources",
        type=str,
        default="hkex_stats,aastocks,wikipedia",
        help="Comma-separated list of sources (default: hkex_stats,aastocks,wikipedia).",
    )
    report_all_p.add_argument(
        "--refresh-sources",
        action="store_true",
        help="Bypass source data cache and re-fetch.",
    )
    report_all_p.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Filter report entries on or after this date (inclusive).",
    )
    report_all_p.add_argument(
        "--until",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Filter report entries on or before this date (inclusive).",
    )
