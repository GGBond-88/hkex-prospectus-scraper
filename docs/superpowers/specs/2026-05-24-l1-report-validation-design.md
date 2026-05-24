# L1 Report & Validation Layer — Design Spec

**Date:** 2026-05-24
**Status:** Draft, pending user approval
**Authors:** brainstorming session with user

---

## 1. Problem statement

The L0 downloader has produced 883 successful PDF downloads against the
manifest at `data/raw_pdfs/manifest.json` for the period 2010-01-01 to
2026-05-22. Investigation of the manifest reveals two problems the user
wants to surface and address:

1. **No roll-up view.** There is no human-readable summary of *what* was
   downloaded per year and per month, only a JSON manifest with 1,847 ticker
   entries.
2. **No external validation.** Discovery is the sole source of truth. The
   year-by-year breakdown shows zero successes in 2020-2023 despite 100+
   skipped entries per year, strongly suggesting a filter regression for
   HKEX's post-2020 headline format — but the system has no way to detect
   such a regression on its own.

This spec defines a new "L1" reporting and validation layer that addresses
both problems.

## 2. Goals and non-goals

### Goals

- Produce `./summary.md` at repo root: a human-readable rollup of the
  L0 manifest with totals, per-year sections, per-month subsections, and
  per-IPO ticker/name listings.
- Produce `./gaps.md` at repo root: a cross-validation report comparing the
  manifest's tickers against three independent external IPO lists (HKEX
  official statistics, AAStocks, Wikipedia).
- Produce `data/validation/reconciled/missing_tickers.txt`: a newline-
  delimited list of tickers that ≥2 external sources confirm but the
  manifest is missing or wrongly skipped, suitable for piping into
  `python -m hk_ipo.l0 refresh $(cat ...)`.
- Keep the design single-responsibility per module (matches the existing
  l0 codebase style: `discovery.py`, `downloader.py`, `manifest.py`,
  `filter.py`, `orchestrator.py` each own one concern).
- Cache raw external responses under `data/validation/raw/` so re-runs are
  offline-replayable and we don't hammer external sites.

### Non-goals (deferred)

- Auto-running `refresh` against `missing_tickers.txt` (operator-driven for v1).
- Investigating or fixing the 2020-2024 L0 filter regression itself —
  that's a separate l0 bug, not part of this reporting layer.
- Differential reports (diff between today's summary and yesterday's).
- Web UI or HTML output.
- Paid data sources (Wind, Bloomberg, S&P Capital IQ).

## 3. Architecture

New module layer `src/hk_ipo/l1/` reads l0's manifest plus external sources
and produces reports. Modules expose plain Python functions; `__main__.py`
is the single CLI entry point — same pattern as l0
(`orchestrator.py:138-225` → `__main__.py:89-97`).

```
src/hk_ipo/l1/
  __init__.py
  models.py              # NormalizedEntry, ExternalIPO, GapReport dataclasses
  manifest_reader.py     # read manifest.json -> list[NormalizedEntry]
  summary_writer.py      # list[NormalizedEntry] -> summary.md
  source_hkex_stats.py   # fetch HKEX market-stats pages -> list[ExternalIPO]
  source_aastocks.py     # scrape AAStocks IPO list -> list[ExternalIPO]
  source_wikipedia.py    # parse Wikipedia year pages -> list[ExternalIPO]
  source_yfinance.py     # per-ticker existence probe (tiebreaker helper)
  reconciler.py          # join manifest + sources -> GapReport
  gaps_writer.py         # GapReport -> gaps.md + missing_tickers.txt + gaps.json
  _http.py               # shared httpx + tenacity + 429/Retry-After envelope
  pipeline.py            # orchestrate stages
  cli.py                 # argparse subcommand handlers wired from __main__.py

src/hk_ipo/l0/__main__.py  # extended with: report | validate | report-all
```

### Generated artifact tree

```
data/validation/
  raw/
    hkex_stats/<year>.html
    aastocks/page_<n>.html
    wikipedia/<year>.html
  normalized/
    manifest_entries.json
    hkex_stats.json
    aastocks.json
    wikipedia.json
  reconciled/
    gaps.json
    missing_tickers.txt
  source_errors.json          # per-source failure log (mirrors discovery's failed_windows.json)

./summary.md                  # repo root — .gitignored
./gaps.md                     # repo root — .gitignored
```

### Module boundaries

- Each `source_*.py` module returns the **same** `list[ExternalIPO]` shape.
  The reconciler does not know which source produced its input.
- All HTTP traffic flows through `_http.py`, which clones the proven
  retry/pacing envelope from `discovery.py:411-442` (httpx + tenacity
  exponential 1-8s on 5xx + transport errors; 3 escalating backoffs on 429
  using Retry-After; 1.5s inter-request pacing).
- Reconciler is a pure function: `reconcile(manifest, sources) -> GapReport`.
  No I/O, trivially unit-testable.
- Pipeline catches per-source exceptions and writes an empty list +
  `source_errors.json` entry. A failure in one source does not block others.

### Cache freshness policy

For each external source, before fetching:
- If `data/validation/raw/<source>/<key>.html` exists AND its mtime is
  less than **7 days old**, use the cached file.
- Otherwise re-fetch and overwrite the cache.
- `--refresh-sources` forces re-fetch regardless of mtime.
- The 7-day window is a constant in `_http.py` (`CACHE_TTL_DAYS = 7`) and
  is documented; not currently configurable via CLI (YAGNI).

## 4. Data shapes

```python
# src/hk_ipo/l1/models.py

@dataclass(frozen=True, slots=True)
class NormalizedEntry:
    """One row of the L0 manifest, normalized for reporting."""
    hk_ticker: str               # 5-digit padded
    status: str                  # success | skipped_wrong_doc_type
                                 # | skipped_no_english | failed
    year: int                    # parsed from doc_url path /YYYY/MMDD/
    month: int
    company_name_en: str | None
    doc_url: str | None
    file_path: str | None        # populated only when status == "success"

@dataclass(frozen=True, slots=True)
class ExternalIPO:
    """One IPO as reported by an external source."""
    hk_ticker: str               # 5-digit padded
    company_name: str
    list_date: date | None       # may be year-only for some sources
    source: str                  # "hkex_stats" | "aastocks" | "wikipedia"
    source_url: str              # provenance link

@dataclass(frozen=True, slots=True)
class GapReport:
    period: tuple[date, date]
    manifest_success: set[str]
    manifest_skipped: dict[str, str]                 # ticker -> skip_reason
    by_source: dict[str, set[str]]                   # source name -> tickers
    missing_from_manifest: set[str]                  # confirmed by >=2 sources
    wrongly_skipped: set[str]                        # confirmed by >=2 sources
                                                     #   AND manifest has skipped_*
    extra_in_manifest: set[str]                      # we downloaded, no source confirms
    single_source_candidates: set[str]               # 1 source only — human triage
    per_year_counts: dict[int, dict[str, int]]       # year -> {source -> count}
```

### URL → year/month parsing

`manifest_reader.py` must handle both URL patterns found in the manifest:

- `/listedco/listconews/sehk/YYYY/MMDD/...` — Main Board
- `/listedco/listconews/gem/YYYY/MMDD/...` — GEM

Regex: `/listconews/(?:sehk|gem)/(\d{4})/(\d{2})(\d{2})/`. The initial
investigation found 272 entries that failed a `/sehk/` -only regex; they
were all GEM. A combined regex matches 100% of entries in the current
manifest.

## 5. Reconciliation logic

```python
def reconcile(
    manifest: list[NormalizedEntry],
    sources: dict[str, list[ExternalIPO]],
) -> GapReport:
    manifest_success = {e.hk_ticker for e in manifest if e.status == "success"}
    manifest_skipped = {
        e.hk_ticker: e.status
        for e in manifest if e.status.startswith("skipped")
    }
    manifest_all = manifest_success | set(manifest_skipped)

    by_source = {
        name: {ipo.hk_ticker for ipo in ipos}
        for name, ipos in sources.items()
    }

    ticker_source_count: Counter[str] = Counter()
    for tickers in by_source.values():
        for t in tickers:
            ticker_source_count[t] += 1
    confirmed = {t for t, n in ticker_source_count.items() if n >= 2}

    return GapReport(
        period=...,
        manifest_success=manifest_success,
        manifest_skipped=manifest_skipped,
        by_source=by_source,
        missing_from_manifest=confirmed - manifest_all,
        wrongly_skipped=confirmed & set(manifest_skipped),
        extra_in_manifest=manifest_success - set().union(*by_source.values()),
        single_source_candidates={
            t for t, n in ticker_source_count.items() if n == 1
        } - manifest_all,
        per_year_counts=_compute_per_year_counts(manifest, sources),
    )
```

### Two-source-agreement rule

A ticker enters `missing_from_manifest` or `wrongly_skipped` (and therefore
`missing_tickers.txt`) only if **≥2 distinct external sources** list it.
Single-source hits are reported under `single_source_candidates` in
`gaps.md` for human triage but do not feed the refresh loop.

This rule is the design's primary noise tolerance: it prevents one bad
scrape from polluting the feedback loop.

### Degraded mode: fewer than 2 sources available

If only 1 source returns data (others all failed), the reconciler still
produces a GapReport but `missing_from_manifest` and `wrongly_skipped` are
empty (the rule cannot be satisfied). Single-source hits go to
`single_source_candidates`. The pipeline exits with code 2 and `gaps.md`
gets a banner header:

> ⚠ **Single-source mode — confidence is reduced.** Sources `<name>` failed
> to fetch. Re-run with `--refresh-sources` once those sources are
> reachable. Do not act on `missing_tickers.txt` from this run.

If zero sources return data, the pipeline exits 2 and writes no
`missing_tickers.txt`.

### yfinance tiebreaker

`source_yfinance.py` is **not** a primary source. It exposes one helper:
`ticker_exists_on_yahoo(ticker: str) -> bool`. The reconciler optionally
uses it to upgrade a single-source candidate to "missing" when the lone
external source is corroborated by Yahoo Finance returning valid info for
`<ticker>.HK`. Disabled by default; opt in with `--use-yfinance-tiebreaker`.

## 6. Output formats

### `summary.md`

```markdown
# HKEX IPO Prospectus Download Summary

Generated: 2026-05-24T12:00:00Z
Period covered: 2010-01-01 to 2026-05-22
Manifest: data/raw_pdfs/manifest.json

## Totals

| Metric | Count |
|---|---|
| Successfully downloaded | 883 |
| Skipped (wrong doc type) | 963 |
| Skipped (no English) | 0 |
| Failed | 1 |
| **Total manifest entries** | **1847** |

## 2024

**Downloaded: 9 / Skipped: 41 / Failed: 0**

### 2024-01
Downloaded (1): 00917

| Ticker | Company |
|---|---|
| 00917 | CHINA STATE CONSTRUCTION DEVELOPMENT HOLDINGS |

### 2024-04
Downloaded (3): 02443, 02505, 02522

| Ticker | Company |
|---|---|
| 02443 | ... |
| 02505 | ... |
| 02522 | ... |

## 2023

**Downloaded: 0 / Skipped: 127 / Failed: 0**

> ⚠ No successful downloads this year. See gaps.md for diagnostic.

### 2023-01 — Skipped (12)
Skipped tickers: 00073, 00204, 00261, ...
```

Rendering rules:

1. Years listed newest → oldest.
2. Months with zero events are omitted (not rendered as empty headings).
3. Successful tickers shown in a `Ticker | Company` table (verbosity choice
   confirmed by user).
4. Skipped tickers shown as a comma-separated line under the month, not a
   table — the interesting payload is "which IPOs did we get".
5. Failed tickers shown in full with the error string (rare: 1 in current
   manifest).
6. A year with zero successes gets a callout pointing to `gaps.md`.

### `gaps.md`

```markdown
# HKEX IPO Coverage — Gap Analysis

Generated: 2026-05-24T12:00:00Z
Period covered: 2010-01-01 to 2026-05-22
Sources consulted: hkex_stats, aastocks, wikipedia

## Source agreement by year

| Year | Manifest (success) | HKEX stats | AAStocks | Wikipedia | Status |
|---|---|---|---|---|---|
| 2024 | 9 | 71 | 73 | 65 | ❌ Large gap |
| 2023 | 0 | 70 | 72 | 58 | ❌ Filter regression |
| 2022 | 0 | 90 | 92 | 75 | ❌ Filter regression |
| 2018 | 170 | 218 | 220 | 195 | ⚠ Minor gap |
| 2010 | 70 | 75 | 78 | 60 | ✅ OK |

Status thresholds (manifest success vs median of external counts):
  ≥ 80% → ✅ OK
  50–79% → ⚠ Minor gap
  <  50% → ❌ Large gap

## Missing from manifest — confirmed by ≥2 sources

| Ticker | Company | List date | Confirmed by |
|---|---|---|---|
| 09988 | ALIBABA | 2019-11-26 | hkex_stats, aastocks, wikipedia |
| ... |

(N tickers — written to data/validation/reconciled/missing_tickers.txt)

## Wrongly skipped — manifest says skipped, ≥2 sources say IPO

| Ticker | Manifest status | Company | Confirmed by |
|---|---|---|---|
| 09618 | skipped_wrong_doc_type | JD.COM | hkex_stats, aastocks |
| ... |

(M tickers — also written to missing_tickers.txt)

## Single-source candidates (triage manually)

| Ticker | Company | Source | Notes |
|---|---|---|---|
| ... |

## Extra in manifest — we downloaded, no source confirms

(low priority; usually means renamed company or pre-2010 listing date)

## Source errors

(empty unless a source failed; see data/validation/source_errors.json)
```

### `missing_tickers.txt`

Newline-delimited 5-digit padded tickers, sorted lexicographically. Designed
for direct shell consumption:

```bash
python -m hk_ipo.l0 refresh $(cat data/validation/reconciled/missing_tickers.txt)
```

## 7. CLI surface

Added to `src/hk_ipo/l0/__main__.py`:

```
python -m hk_ipo.l0 report
    [--manifest PATH]                # default: cfg.manifest_path
    [--output ./summary.md]
    [--since YYYY-MM-DD] [--until YYYY-MM-DD]

python -m hk_ipo.l0 validate
    [--manifest PATH]
    [--sources hkex_stats,aastocks,wikipedia]    # default: all three
    [--refresh-sources]                          # force re-fetch (ignore cache)
    [--use-yfinance-tiebreaker]                  # opt in to yfinance helper
    [--output ./gaps.md]
    [--missing-tickers-out data/validation/reconciled/missing_tickers.txt]

python -m hk_ipo.l0 report-all
    # Convenience wrapper: runs report then validate. For cron / CI.
```

Exit codes:
- `0` — success
- `1` — manifest missing or unreadable
- `2` — pipeline degraded (≥1 source failed, single-source mode, or
  parser yielded zero rows from a non-empty response)
- `3` — uncaught exception (matches l0's convention at `__main__.py:124-126`)

## 8. External source modules — implementation strategy

### `source_hkex_stats.py`

- **Target:** HKEX Market Statistics monthly "newly listed companies"
  pages (e.g.
  `https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/HKEX-Monthly-Market-Highlights`).
- **Approach:** one HTTP GET per month, 2010-01 → current month. Parse
  with `selectolax` (existing project dep).
- **Rate-limit handling:** 1.5s inter-request pacing; honor Retry-After
  on 429 (reused from `_http.py`).
- **Cache:** `data/validation/raw/hkex_stats/<YYYY-MM>.html`.

### `source_aastocks.py`

- **Target:**
  `http://www.aastocks.com/en/stocks/market/ipo/listedipo.aspx?s=1&o=0&page=N`.
- **Approach:** walk paginated history. Extract ticker, name, list-date.
- **Risk:** if the table is client-side-rendered, fall back to the mobile
  view or the XHR endpoint. The first task in this module's implementation
  is to determine which URL serves the data server-side. Documented in the
  plan as a small spike.
- **Cache:** `data/validation/raw/aastocks/page_<n>.html`.

### `source_wikipedia.py`

- **Target:**
  `https://en.wikipedia.org/wiki/List_of_initial_public_offerings_on_the_Hong_Kong_Stock_Exchange_in_<YEAR>`
  (URL format varies by year — module must handle the variation).
- **Approach:** use the Wikipedia API
  (`action=parse&page=...&prop=wikitext`) for structured wikitext rather
  than scraping rendered HTML. Parse `{| wikitable` blocks.
- **Coverage caveat:** Wikipedia is incomplete pre-2015 and skews to large
  IPOs. Treated as a confirming signal only — never the sole source.
- **Cache:** `data/validation/raw/wikipedia/<year>.html` (raw API response).

### `source_yfinance.py`

- One function: `ticker_exists_on_yahoo(ticker: str) -> bool`. Returns True
  if `yfinance.Ticker("<ticker>.HK").info` resolves with a non-empty
  `symbol` field.
- Used by the reconciler as a tiebreaker only (see §5).
- `yfinance` added to `requirements.txt`.

### `_http.py`

```python
class ValidationHTTPClient:
    """Thin wrapper around httpx.AsyncClient. Identical retry/pacing
    envelope to discovery.py:411-442 — 5xx + transport errors get
    tenacity exponential 1-8s; 429 gets 3 escalating backoffs honoring
    Retry-After; 1.5s pacing between requests to the same host."""
    def __init__(self, user_agent: str, log_dir: Path, ...): ...
    async def get(self, url: str, *, host_key: str | None = None) -> httpx.Response: ...
```

User-Agent: `build_user_agent(cfg.contact_email)` (reuse the existing
helper at `config.py:72-77`).

## 9. Error handling — explicit policies

| Failure mode | Behavior |
|---|---|
| One external source raises | Log to `data/validation/source_errors.json`; emit empty list for that source; continue pipeline. |
| All sources fail | Pipeline exits 2; `gaps.md` written with banner header; no `missing_tickers.txt`. |
| 1 source survives (others fail) | Pipeline exits 2 (degraded); `gaps.md` written with banner header; no `missing_tickers.txt`. |
| HTTP 429 from any source | `_http.py` envelope handles: Retry-After honored, 3 escalating backoffs (30/60/120s), then fail this source only. |
| Manifest missing | `report` exits 1 with message `"manifest not found at PATH; run backfill first"` (mirrors `run_status` at `orchestrator.py:354-374`). |
| Parser yields zero rows from a non-empty HTML response | Treated as schema drift. Logged to `source_errors.json` as a parser failure. Pipeline exits 2. We **do not** silently produce a clean-looking gaps.md when a parser is broken. |
| `summary.md` / `gaps.md` already exist | Overwritten without prompt. They are generated artifacts. |
| Network offline | `report` works (manifest-only). `validate` reuses `data/validation/raw/` cache if present; if cache is empty AND offline, exits 2. |

## 10. Testing

Mirrors the existing `tests/` patterns (l0 has `test_l0_*.py` for unit
tests and `tests/blackbox/` for end-to-end CLI tests).

- `tests/test_l1_manifest_reader.py` — golden manifest fixture →
  normalized entries match. Covers both `/sehk/YYYY/` and `/gem/YYYY/`
  URL patterns.
- `tests/test_l1_summary_writer.py` — fixture entries → snapshot-test the
  rendered markdown. Verifies year ordering, empty-month omission, callout
  for zero-success years.
- `tests/test_l1_source_hkex_stats.py` — saved HTML fixture →
  assert ExternalIPO list.
- `tests/test_l1_source_aastocks.py` — saved HTML fixture →
  assert ExternalIPO list.
- `tests/test_l1_source_wikipedia.py` — saved wikitext fixture →
  assert ExternalIPO list.
- `tests/test_l1_reconciler.py` — synthetic manifest + 3 synthetic source
  lists → assert set math (two-source-agreement, single-source detection,
  wrongly-skipped) is correct. Also covers degraded modes (0/1 source).
- `tests/test_l1_pipeline.py` — end-to-end with all HTTP mocked via
  fixtures → assert files land in correct paths.
- `tests/blackbox/test_l1_cli_blackbox.py` — invoke `python -m hk_ipo.l0
  report` and `validate` against a stub HTTP server. Reuses the
  `stub_hkex_server.py` pattern at `tests/blackbox/stub_hkex_server.py`.

**Rule (matches existing repo convention):** unit tests must not make
live HTTP requests. All external fetches are fixture-replayed.

## 11. Dependencies

Additions to `requirements.txt` (or equivalent):

- `yfinance` (new — optional tiebreaker)

No other new dependencies. `httpx`, `tenacity`, `selectolax` are already
project dependencies used by l0.

## 12. Open risks

1. **AAStocks page may be client-side rendered.** Mitigation: first
   implementation task for that module is a spike to find a server-side
   endpoint (mobile view, print view, or XHR JSON). If none exists, drop
   AAStocks and substitute another free source (ETNet 經濟通) — the design
   tolerates this because reconciliation only requires ≥2 of N sources.
2. **HKEX statistics page URL structure may change.** Mitigation: archive
   raw HTML, fail loud on parser drift (see §9). Operator can update the
   parser without losing historical cached data.
3. **Wikipedia per-year page naming inconsistency.** Mitigation:
   `source_wikipedia.py` accepts a list of candidate URL patterns per
   year; if all fail, that year is logged as missing in
   `source_errors.json`.
4. **External sources may use Traditional Chinese names while the
   manifest has English.** Reconciliation is by ticker (numeric), not by
   name — this risk is contained.

## 13. Build order (preview for the implementation plan)

1. `models.py` + `manifest_reader.py` + tests
2. `summary_writer.py` + tests → ship `report` subcommand
3. `_http.py` + tests
4. `source_hkex_stats.py` + fixture + test
5. `source_aastocks.py` + spike + fixture + test
6. `source_wikipedia.py` + fixture + test
7. `reconciler.py` + tests
8. `gaps_writer.py` + tests
9. `pipeline.py` + `cli.py` + blackbox test → ship `validate` and
   `report-all`
10. `source_yfinance.py` + tiebreaker wiring (last; gated by feedback)

Stages 1-2 are independently shippable (summary.md without validation).
Stages 3-9 land the full feature.
