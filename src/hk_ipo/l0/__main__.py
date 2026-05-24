"""CLI entry point: python -m hk_ipo.l0 <subcommand>"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime

from hk_ipo.config import Config
from hk_ipo.logging_setup import configure_run_logger
from hk_ipo.l0.orchestrator import (
    run_backfill,
    run_refresh,
    run_retry_failed,
    run_status,
    run_sync,
    run_verify,
)
from hk_ipo.l1.cli import add_l1_subparsers
from hk_ipo.l1.pipeline import run_report, run_report_all, run_validate


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hk_ipo.l0",
        description="HKEX IPO prospectus auto-downloader (L0).",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--manifest", type=str, default=None,
                   help="Override manifest path (default: $HK_IPO_DATA_DIR/raw_pdfs/manifest.json)")

    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backfill", help="Historical backfill by date range.")
    pb.add_argument("--since", type=_parse_date, default=None)
    pb.add_argument("--until", type=_parse_date, default=None)
    pb.add_argument("--workers", type=int, default=None)
    pb.add_argument("--dry-run", action="store_true")
    pb.add_argument("--limit", type=int, default=None)

    ps = sub.add_parser("sync", help="Incremental sync since last successful run.")
    ps.add_argument("--workers", type=int, default=None)
    ps.add_argument("--dry-run", action="store_true")

    pr = sub.add_parser("retry-failed", help="Re-attempt failed entries.")
    pr.add_argument("--workers", type=int, default=None)
    pr.add_argument("--max-age-days", type=int, default=30)

    pf = sub.add_parser("refresh", help="Force re-download for specific tickers.")
    pf.add_argument("tickers", nargs="+")
    pf.add_argument("--workers", type=int, default=None)

    pst = sub.add_parser("status", help="Manifest counts.")
    pst.add_argument("--json", action="store_true", dest="json_mode")

    pv = sub.add_parser("verify", help="Re-hash on-disk PDFs against the manifest.")
    pv.add_argument("--repair", action="store_true")

    add_l1_subparsers(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = Config.from_env()
    if args.manifest:
        from pathlib import Path
        cfg = Config(
            data_dir=cfg.data_dir,
            raw_pdfs_dir=cfg.raw_pdfs_dir,
            manifest_path=Path(args.manifest),
            log_dir=cfg.log_dir,
            json_api_base=cfg.json_api_base,
            html_search_base=cfg.html_search_base,
            partial_lookup_base=cfg.partial_lookup_base,
            pdf_base=cfg.pdf_base,
            contact_email=cfg.contact_email,
            default_workers=cfg.default_workers,
            backfill_start_date=cfg.backfill_start_date,
        )
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    configure_run_logger(cfg.log_dir, run_id=run_id, level=args.log_level)

    workers = getattr(args, "workers", None) or cfg.default_workers

    start = time.perf_counter()
    try:
        if args.cmd == "backfill":
            since = args.since or _parse_date(cfg.backfill_start_date)
            until = args.until or date.today()
            summary = asyncio.run(run_backfill(
                cfg, since=since, until=until, workers=workers,
                dry_run=args.dry_run, limit=args.limit,
            ))
            print(summary.render(time.perf_counter() - start))
            return 0
        if args.cmd == "sync":
            summary = asyncio.run(run_sync(
                cfg, workers=workers, dry_run=args.dry_run,
            ))
            print(summary.render(time.perf_counter() - start))
            return 0
        if args.cmd == "retry-failed":
            summary = asyncio.run(run_retry_failed(
                cfg, workers=workers, max_age_days=args.max_age_days,
            ))
            print(summary.render(time.perf_counter() - start))
            return 0
        if args.cmd == "refresh":
            summary = asyncio.run(run_refresh(
                cfg, tickers=args.tickers, workers=workers,
            ))
            print(summary.render(time.perf_counter() - start))
            return 0 if summary.failed == 0 else 1
        if args.cmd == "status":
            print(run_status(cfg, json_mode=args.json_mode))
            return 0
        if args.cmd == "verify":
            code, drifted = run_verify(cfg, repair=args.repair)
            if drifted:
                print(f"drift detected on: {', '.join(drifted)}", file=sys.stderr)
            return code
        if args.cmd == "report":
            manifest = getattr(args, "manifest", None) or cfg.manifest_path
            code = asyncio.run(run_report(
                manifest,
                getattr(args, "output", Path("./summary.md")),
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
            ))
            return code
        if args.cmd == "validate":
            manifest = getattr(args, "manifest", None) or cfg.manifest_path
            sources_list = [
                s.strip() for s in getattr(args, "sources", "hkex_stats,aastocks,wikipedia").split(",")
            ]
            code = asyncio.run(run_validate(
                manifest,
                getattr(args, "output", Path("./gaps.md")),
                getattr(args, "missing_tickers_out", Path("data/validation/reconciled/missing_tickers.txt")),
                sources=sources_list,
                refresh_sources=getattr(args, "refresh_sources", False),
                contact_email=cfg.contact_email,
            ))
            return code
        if args.cmd == "report-all":
            manifest = getattr(args, "manifest", None) or cfg.manifest_path
            sources_list = [
                s.strip() for s in getattr(args, "sources", "hkex_stats,aastocks,wikipedia").split(",")
            ]
            code = asyncio.run(run_report_all(
                manifest,
                getattr(args, "summary", Path("./summary.md")),
                getattr(args, "gaps", Path("./gaps.md")),
                getattr(args, "missing_tickers_out", Path("data/validation/reconciled/missing_tickers.txt")),
                sources=sources_list,
                refresh_sources=getattr(args, "refresh_sources", False),
                contact_email=cfg.contact_email,
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
            ))
            return code
    except Exception as e:  # pragma: no cover - defensive top-level
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    return 4  # unreachable


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
