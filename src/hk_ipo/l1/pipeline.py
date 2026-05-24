"""Orchestration functions that chain the L1 stages into runnable pipelines."""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from hk_ipo.config import build_user_agent
from hk_ipo.l1._http import ValidationHTTPClient
from hk_ipo.l1.gaps_writer import write_gaps, write_gaps_json, write_missing_tickers
from hk_ipo.l1.manifest_reader import read_manifest
from hk_ipo.l1.models import NormalizedEntry
from hk_ipo.l1.reconciler import reconcile
from hk_ipo.l1.source_aastocks import SOURCE_NAME as AASTOCKS, fetch_aastocks
from hk_ipo.l1.source_hkex_stats import SOURCE_NAME as HKEX_STATS, fetch_hkex_stats
from hk_ipo.l1.source_wikipedia import SOURCE_NAME as WIKIPEDIA, fetch_wikipedia
from hk_ipo.l1.summary_writer import write_summary

logger = logging.getLogger("hk_ipo")

DEFAULT_SOURCES = [HKEX_STATS, AASTOCKS, WIKIPEDIA]


# ---------------------------------------------------------------------------
# run_report
# ---------------------------------------------------------------------------


async def run_report(
    manifest_path: Path,
    output_path: Path,
    *,
    since: date | None = None,
    until: date | None = None,
) -> int:
    """Run summary report pipeline. Returns exit code.

    1. read_manifest -> list[NormalizedEntry]
    2. filter by since/until if provided
    3. write_summary -> summary.md
    """
    if not manifest_path.exists():
        print(
            f"ERROR: manifest not found at {manifest_path}",
            file=sys.stderr,
        )
        return 1

    logger.info("Reading manifest: %s", manifest_path)
    entries = read_manifest(manifest_path)

    if since is not None or until is not None:
        entries = _filter_by_date(entries, since=since, until=until)
        logger.info(
            "Filtered to %d entries (since=%s, until=%s).",
            len(entries), since, until,
        )

    logger.info("Writing summary to %s", output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_summary(entries, output_path, manifest_path)

    return 0


# ---------------------------------------------------------------------------
# run_validate
# ---------------------------------------------------------------------------


async def run_validate(
    manifest_path: Path,
    output_path: Path,
    missing_tickers_path: Path,
    *,
    sources: list[str] | None = None,
    refresh_sources: bool = False,
    contact_email: str = "",
) -> int:
    """Run validation pipeline. Returns exit code.

    1. read_manifest -> list[NormalizedEntry]
    2. Fetch each enabled source (in sequence, respecting pacing)
    3. reconcile(manifest, sources) -> GapReport
    4. write_gaps(), write_missing_tickers(), write_gaps_json()

    Returns 0 on success, 2 if degraded (0-1 sources have data).
    """
    if sources is None:
        sources = DEFAULT_SOURCES

    if not manifest_path.exists():
        print(
            f"ERROR: manifest not found at {manifest_path}",
            file=sys.stderr,
        )
        return 1

    logger.info("Reading manifest: %s", manifest_path)
    entries = read_manifest(manifest_path)

    user_agent = build_user_agent(contact_email)
    sources_data: dict[str, list] = {}
    successful_sources = 0

    async with ValidationHTTPClient(user_agent=user_agent) as client:
        for source_name in sources:
            try:
                if source_name == HKEX_STATS:
                    data = await fetch_hkex_stats(
                        client, force_refresh=refresh_sources,
                    )
                elif source_name == AASTOCKS:
                    data = await fetch_aastocks(
                        client, force_refresh=refresh_sources,
                    )
                elif source_name == WIKIPEDIA:
                    data = await fetch_wikipedia(
                        client, force_refresh=refresh_sources,
                    )
                else:
                    logger.warning("Unknown source: %s; skipping.", source_name)
                    continue
            except Exception:
                logger.exception(
                    "Source '%s' raised an exception; skipping.", source_name,
                )
                _log_source_error(source_name, f"Exception during fetch for {source_name}")
                continue

            if data:
                sources_data[source_name] = data
                successful_sources += 1
                logger.info(
                    "Source '%s' returned %d IPOs.", source_name, len(data),
                )
            else:
                logger.warning(
                    "Source '%s' returned no data.", source_name,
                )
                # Still include empty source so the reconciler knows about it
                sources_data[source_name] = data

    # reconcile
    logger.info(
        "Reconciling manifest (%d entries) against %d sources.",
        len(entries), len(sources_data),
    )
    report = reconcile(entries, sources_data)

    # write outputs
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_gaps(report, output_path)
    write_missing_tickers(report, missing_tickers_path)
    gaps_json_path = output_path.with_suffix(".json")
    write_gaps_json(report, gaps_json_path)

    # Determine exit code based on degradation
    if successful_sources < 2:
        logger.warning(
            "Degraded: only %d source(s) provided data. Exit code 2.",
            successful_sources,
        )
        return 2

    return 0


# ---------------------------------------------------------------------------
# run_report_all
# ---------------------------------------------------------------------------


async def run_report_all(
    manifest_path: Path,
    summary_path: Path,
    gaps_path: Path,
    missing_tickers_path: Path,
    **kwargs,
) -> int:
    """Run report + validate in sequence. Returns max of both exit codes."""
    report_code = await run_report(
        manifest_path, summary_path,
        since=kwargs.pop("since", None),
        until=kwargs.pop("until", None),
    )

    validate_code = await run_validate(
        manifest_path, gaps_path, missing_tickers_path,
        sources=kwargs.pop("sources", None),
        refresh_sources=kwargs.pop("refresh_sources", False),
        contact_email=kwargs.pop("contact_email", ""),
    )

    return max(report_code, validate_code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_by_date(
    entries: list[NormalizedEntry],
    *,
    since: date | None = None,
    until: date | None = None,
) -> list[NormalizedEntry]:
    """Filter entries to those whose year/month falls within [since, until].

    An entry is included when its first day of the month is in the range
    (inclusive on both ends). Entries with year=0 are included only when
    both since and until are None (no filtering applied).
    """
    filtered: list[NormalizedEntry] = []
    for e in entries:
        if e.year == 0:
            continue
        entry_first = date(e.year, e.month, 1)
        if since is not None and entry_first < since.replace(day=1):
            continue
        if until is not None and entry_first > until.replace(day=1):
            continue
        filtered.append(e)
    return filtered


def _log_source_error(source_name: str, reason: str) -> None:
    """Append a pipeline-level source error to source_errors.json.

    This is separate from the per-module _log_source_error functions;
    it catches top-level exceptions from the fetch_* calls.
    """
    err_dir = Path("data/validation")
    err_path = err_dir / "source_errors.json"
    err_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "source": source_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }

    existing: list[dict] = []
    if err_path.exists():
        try:
            existing = json.loads(err_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(entry)
    existing = existing[-50:]  # cap at 50

    err_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
