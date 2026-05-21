# L0 acceptance playbook

This document records manual acceptance steps that cannot be fully automated
in the current repo (no L1 module yet -- per SI-004).

## Automated criteria (covered by tests)

- AC 1: `tests/e2e/test_l0_e2e.py::test_live_backfill_h1_2024_meets_30_filings_threshold`
- AC 2: `tests/blackbox/test_l0_cli_blackbox.py::test_backfill_limit_5_downloads_exactly_five_and_verifies_manifest`
- AC 3: `tests/blackbox/test_l0_cli_blackbox.py::test_backfill_idempotent_on_rerun`
- AC 4: `tests/blackbox/test_l0_cli_blackbox.py::test_status_reports_counts`
- AC 5: `tests/blackbox/test_l0_cli_blackbox.py::test_verify_passes_on_intact_files`
- AC 6: `tests/test_l0_golden_fixtures.py` (all parametrized cases)
- AC 7: every `pytest` invocation
- AC 8: `tests/blackbox/test_l0_cli_blackbox.py::test_orphan_tmp_file_cleaned_on_startup`

## AC 9: L0 -> L1 contract (deferred per SI-004)

When L1 lands in this repo, run:

    python -m hk_ipo.l0 backfill --since 2024-01-01 --limit 5
    python -m hk_ipo.l1_sectioning data/raw_pdfs/<one-of-the-tickers>.pdf

Verify that `data/sections/<ticker>.json` is produced and parses cleanly.

Until then, the L0 contract is verified at the file-system level:

  - `data/raw_pdfs/<5-digit-ticker>.pdf` exists.
  - The file opens in a PDF reader and contains the prospectus content.
  - `data/raw_pdfs/manifest.json` lists the ticker with status=success.

## Known environment issues

### EI-001: HKEX Title Search API reorganized; IPO prospectuses no longer available

The JSON API endpoint `titlesearchservlet.do` used by the discovery module now
returns HTTP 404. HKEX has replaced it with `titleSearchServlet.do` which uses
different parameter names and response schema. However, the new API no longer
includes IPO prospectuses — the document categorization system has been
completely reorganized (t1code/t2code taxonomy changed), and IPO prospectuses
for actual company listings do not appear in any response from the new API.

The e2e tests (`tests/e2e/test_l0_e2e.py`) and golden-fixture tests
(`tests/test_l0_golden_fixtures.py`) will fail against live HKEX until a new
API endpoint serving IPO prospectus data is identified (likely a different
endpoint than `titleSearchServlet.do`). All mock and stub-based tests continue
to pass.

See `working/env-issues.md` for the full reverse-engineering investigation
details, including the correct new API parameters confirmed from JS source.
