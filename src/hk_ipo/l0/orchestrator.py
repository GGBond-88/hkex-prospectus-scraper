"""High-level orchestrators called by the CLI. One coroutine per subcommand."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from hk_ipo.config import Config, build_user_agent
from hk_ipo.l0.discovery import HKEXDiscoveryClient
from hk_ipo.l0.downloader import PDFDownloader, sweep_orphan_tmp_files
from hk_ipo.l0.filter import FilterDecision, SkipReason, should_skip
from hk_ipo.l0.manifest import ManifestStore
from hk_ipo.l0.models import (
    DownloadOutcome,
    Filing,
    ManifestStatus,
    pad_ticker,
)

logger = logging.getLogger("hk_ipo")


@dataclass(slots=True)
class RunSummary:
    discovered: int = 0
    downloaded: int = 0
    skipped_no_english: int = 0
    skipped_wrong_doc_type: int = 0
    skipped_already_have: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    failed_tickers: list[str] = field(default_factory=list)

    def render(self, elapsed_seconds: float) -> str:
        return (
            f"L0 sync complete (elapsed {elapsed_seconds:.1f}s)\n"
            f"  Discovered:      {self.discovered}\n"
            f"  Downloaded:      {self.downloaded} new PDFs"
            f" ({self.bytes_downloaded / 1024 / 1024:.1f} MB)\n"
            f"  Skipped:         {self.skipped_already_have + self.skipped_no_english}"
            f" (already-have: {self.skipped_already_have},"
            f" no-english: {self.skipped_no_english})\n"
            f"  Failed:          {self.failed}"
        )


# --------------------------------------------------------------------------- #
# Shared download routine
# --------------------------------------------------------------------------- #

async def _download_filings(
    cfg: Config,
    filings: Iterable[Filing],
    store: ManifestStore,
    summary: RunSummary,
    *,
    workers: int,
    force_overwrite: bool = False,
) -> None:
    filings = list(filings)
    if not filings:
        return
    sweep_orphan_tmp_files(cfg.raw_pdfs_dir)
    ua = build_user_agent(cfg.contact_email)
    async with PDFDownloader(
        raw_pdfs_dir=cfg.raw_pdfs_dir,
        user_agent=ua,
        max_workers=workers,
    ) as dl:
        results = await dl.download_many(filings)
    by_ticker = {f.hk_ticker: f for f in filings}
    non_success_since_last_save = 0
    for r in results:
        f = by_ticker[r.hk_ticker]
        if r.outcome == DownloadOutcome.SUCCESS:
            store.add_success(
                hk_ticker=r.hk_ticker,
                doc_id=f.doc_id, doc_url=f.doc_url, doc_title=f.doc_title,
                file_path=r.file_path or f"{r.hk_ticker}.pdf",
                file_sha256=r.file_sha256 or "",
                file_size_bytes=r.file_size_bytes or 0,
                discovered_at=datetime.now(tz=timezone.utc),
                market=f.market, language=f.language,
                company_name_en=f.company_name_en,
                company_name_zh=f.company_name_zh,
            )
            summary.downloaded += 1
            summary.bytes_downloaded += r.file_size_bytes or 0
            store.save()  # persist after every success (spec section 5)
            non_success_since_last_save = 0
        else:
            store.mark_failed(
                hk_ticker=r.hk_ticker,
                doc_url=f.doc_url,
                error=r.error or "unknown",
                discovered_at=datetime.now(tz=timezone.utc),
            )
            summary.failed += 1
            summary.failed_tickers.append(r.hk_ticker)
            non_success_since_last_save += 1
            if non_success_since_last_save >= 10:
                store.save()  # persist every 10 failures (spec section 5)
                non_success_since_last_save = 0
    if non_success_since_last_save > 0:
        store.save()  # flush remaining non-success entries


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

async def run_backfill(
    cfg: Config, *, since: date, until: date,
    workers: int, dry_run: bool, limit: int | None,
) -> RunSummary:
    summary = RunSummary()
    store = ManifestStore.load(cfg.manifest_path) if not dry_run else None

    keep: list[Filing] = []
    discovery_skips_since_last_save = 0
    async with HKEXDiscoveryClient(
        json_api_base=cfg.json_api_base,
        html_search_base=cfg.html_search_base,
        pdf_base_url=cfg.pdf_base,
        log_dir=cfg.log_dir,
        user_agent=build_user_agent(cfg.contact_email),
        inter_window_sleep=1.5,  # spec section 7 pacing
    ) as disc:
        async for filing in disc.list_filings(since, until):
            summary.discovered += 1
            decision = FilterDecision.from_filing(filing)
            if not decision.keep:
                if decision.skip_reason == SkipReason.NO_ENGLISH:
                    summary.skipped_no_english += 1
                    if store is not None:
                        store.mark_skipped_no_english(
                            hk_ticker=filing.hk_ticker,
                            doc_id=filing.doc_id, doc_url=filing.doc_url,
                            company_name_zh=filing.company_name_zh,
                            company_name_en=filing.company_name_en,
                            discovered_at=datetime.now(tz=timezone.utc),
                        )
                else:
                    summary.skipped_wrong_doc_type += 1
                    if store is not None:
                        store.mark_skipped_wrong_doc_type(
                            hk_ticker=filing.hk_ticker,
                            doc_id=filing.doc_id, doc_url=filing.doc_url,
                            discovered_at=datetime.now(tz=timezone.utc),
                        )
                if store is not None:
                    discovery_skips_since_last_save += 1
                    if discovery_skips_since_last_save >= 10:
                        store.save()  # persist every 10 skips (spec section 5)
                        discovery_skips_since_last_save = 0
                continue
            # Check if we already have it.
            if store is not None:
                existing = store.get_status(filing.hk_ticker)
                if existing in (
                    ManifestStatus.SUCCESS,
                    ManifestStatus.SKIPPED_NO_ENGLISH,
                    ManifestStatus.SKIPPED_WRONG_DOC_TYPE,
                    ManifestStatus.FAILED,
                ):
                    summary.skipped_already_have += 1
                    continue
            keep.append(filing)

    if limit is not None:
        keep = keep[:limit]

    # Flush remaining discovery skips that didn't hit the batch threshold.
    if store is not None and discovery_skips_since_last_save > 0:
        store.save()

    if dry_run:
        for f in keep:
            print(f"DRY-RUN would download {f.hk_ticker} {f.doc_title} <{f.doc_url}>")
        return summary

    assert store is not None
    await _download_filings(cfg, keep, store, summary, workers=workers)
    store.last_full_sync = datetime.now(tz=timezone.utc)
    store.last_incremental_sync = store.last_full_sync
    store.save()
    return summary


async def run_sync(cfg: Config, *, workers: int, dry_run: bool) -> RunSummary:
    store = ManifestStore.load(cfg.manifest_path)
    since_dt = store.last_incremental_sync or datetime.now(tz=timezone.utc).replace(
        year=datetime.now().year - 1,
    )
    until_dt = datetime.now(tz=timezone.utc)
    summary = await run_backfill(
        cfg, since=since_dt.date(), until=until_dt.date(),
        workers=workers, dry_run=dry_run, limit=None,
    )
    if not dry_run:
        store2 = ManifestStore.load(cfg.manifest_path)
        store2.last_incremental_sync = until_dt
        store2.save()
    return summary


async def run_retry_failed(
    cfg: Config, *, workers: int, max_age_days: int | None,
) -> RunSummary:
    summary = RunSummary()
    store = ManifestStore.load(cfg.manifest_path)
    failed_entries = list(store.failed_entries(max_age_days=max_age_days))
    if not failed_entries:
        return summary
    # Rebuild a Filing from the manifest row enough to redo the download.
    from datetime import datetime as _dt
    filings: list[Filing] = []
    for e in failed_entries:
        if not e.doc_url:
            continue
        filings.append(Filing(
            hk_ticker=e.hk_ticker,
            doc_id=e.doc_id or "unknown",
            doc_title=e.doc_title or "Listing Document",
            doc_url=e.doc_url,
            doc_type=e.doc_type if False else "Prospectus",  # market unknown -> assume MB
            market="MB",
            language="en",
            is_final=True,
            publish_date=e.discovered_at or _dt.now(tz=timezone.utc),
        ))
        summary.discovered += 1
    await _download_filings(cfg, filings, store, summary, workers=workers)
    return summary


async def run_refresh(
    cfg: Config, tickers: list[str], *, workers: int,
) -> RunSummary:
    summary = RunSummary()
    store = ManifestStore.load(cfg.manifest_path)
    filings: list[Filing] = []
    for raw in tickers:
        ticker = pad_ticker(raw)
        try:
            existing = store.get(ticker)
        except KeyError:
            summary.failed += 1
            summary.failed_tickers.append(ticker)
            continue
        if not existing.doc_url:
            summary.failed += 1
            summary.failed_tickers.append(ticker)
            continue

        # PR-002: Re-evaluate filter for skipped entries.
        if existing.status == ManifestStatus.SKIPPED_NO_ENGLISH:
            # Try English URL variant (swap _c.pdf -> _e.pdf) and re-evaluate filter.
            english_url = existing.doc_url.replace("_c.pdf", "_e.pdf")
            candidate = Filing(
                hk_ticker=ticker,
                doc_id=existing.doc_id or "unknown",
                doc_title=existing.doc_title or "Listing Document",
                doc_url=english_url,
                doc_type="Prospectus",
                market=existing.market or "MB",
                language="en",
                is_final=True,
                publish_date=existing.discovered_at or datetime.now(tz=timezone.utc),
            )
            if should_skip(candidate) is not None:
                summary.skipped_no_english += 1
                continue
            filings.append(candidate)
            summary.discovered += 1
            continue

        # PR-010: Re-evaluate filter for skipped_wrong_doc_type entries.
        if existing.status == ManifestStatus.SKIPPED_WRONG_DOC_TYPE:
            candidate = Filing(
                hk_ticker=ticker,
                doc_id=existing.doc_id or "unknown",
                doc_title=existing.doc_title or "Listing Document",
                doc_url=existing.doc_url,
                doc_type="Prospectus",
                market=existing.market or "MB",
                language="en",
                is_final=True,
                publish_date=existing.discovered_at or datetime.now(tz=timezone.utc),
            )
            reason = should_skip(candidate)
            if reason == SkipReason.WRONG_DOC_TYPE:
                summary.skipped_wrong_doc_type += 1
                continue
            filings.append(candidate)
            summary.discovered += 1
            continue

        filings.append(Filing(
            hk_ticker=ticker,
            doc_id=existing.doc_id or "unknown",
            doc_title=existing.doc_title or "Listing Document",
            doc_url=existing.doc_url,
            doc_type="Prospectus",
            market=existing.market or "MB",
            language="en",
            is_final=True,
            publish_date=existing.discovered_at or datetime.now(tz=timezone.utc),
        ))
        summary.discovered += 1
    await _download_filings(cfg, filings, store, summary, workers=workers,
                            force_overwrite=True)
    return summary


def run_status(cfg: Config, *, json_mode: bool = False) -> str:
    if not cfg.manifest_path.exists():
        if json_mode:
            return '{"success": 0, "skipped": 0, "failed": 0, "pending": 0}'
        return "manifest is empty"
    store = ManifestStore.load(cfg.manifest_path)
    counts: dict[str, int] = {s.value: 0 for s in ManifestStatus}
    for ticker in [t for t in store.iter_tickers()]:
        counts[store.get(ticker).status.value] += 1
    if json_mode:
        import json as _json
        return _json.dumps(counts, sort_keys=True)
    lines = [
        f"manifest: {sum(counts.values())} entries",
        f"  success:                {counts['success']}",
        f"  skipped_no_english:     {counts['skipped_no_english']}",
        f"  skipped_wrong_doc_type: {counts['skipped_wrong_doc_type']}",
        f"  failed:                 {counts['failed']}",
        f"  pending:                {counts['pending']}",
    ]
    return "\n".join(lines)


def run_verify(cfg: Config, *, repair: bool = False) -> tuple[int, list[str]]:
    """Return (exit_code, list_of_drift_tickers).

    If repair=True, drifted tickers are re-downloaded (via run_refresh)
    and then re-verified. Exit code 0 means all files match.
    """
    if not cfg.manifest_path.exists():
        return 0, []
    import hashlib as _hashlib
    store = ManifestStore.load(cfg.manifest_path)

    def _find_drifted() -> list[str]:
        drifted: list[str] = []
        for ticker in store.iter_tickers():
            entry = store.get(ticker)
            if entry.status != ManifestStatus.SUCCESS or not entry.file_path:
                continue
            path = cfg.raw_pdfs_dir / entry.file_path
            if not path.exists():
                drifted.append(ticker)
                continue
            h = _hashlib.sha256(path.read_bytes()).hexdigest()
            if h != entry.file_sha256:
                drifted.append(ticker)
        return drifted

    drifted = _find_drifted()

    if repair and drifted:
        import asyncio as _aio
        _aio.run(run_refresh(cfg, drifted, workers=cfg.default_workers))
        drifted = _find_drifted()  # re-verify after repair

    return (0 if not drifted else 2), drifted
