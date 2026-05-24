"""Frozen dataclasses for the L1 report and validation layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from hk_ipo.l0.models import pad_ticker


@dataclass(frozen=True, slots=True)
class NormalizedEntry:
    """One row of the L0 manifest, normalized for reporting."""

    hk_ticker: str  # 5-digit padded
    status: str  # success | skipped_wrong_doc_type | skipped_no_english | failed
    year: int  # parsed from doc_url path /YYYY/MMDD/
    month: int
    company_name_en: str | None
    doc_url: str | None
    file_path: str | None  # populated only when status == "success"
    error_msg: str | None = None  # populated for failed entries

    def __post_init__(self) -> None:
        object.__setattr__(self, "hk_ticker", pad_ticker(self.hk_ticker))


@dataclass(frozen=True, slots=True)
class ExternalIPO:
    """One IPO as reported by an external source."""

    hk_ticker: str  # 5-digit padded
    company_name: str
    list_date: date | None  # may be year-only for some sources
    source: str  # "hkex_stats" | "aastocks" | "wikipedia"
    source_url: str  # provenance link

    def __post_init__(self) -> None:
        object.__setattr__(self, "hk_ticker", pad_ticker(self.hk_ticker))


@dataclass(frozen=True, slots=True)
class GapReport:
    period: tuple[date, date]
    manifest_success: set[str]
    manifest_skipped: dict[str, str]  # ticker -> skip_reason
    by_source: dict[str, set[str]]  # source name -> tickers
    missing_from_manifest: set[str]  # confirmed by >=2 sources
    wrongly_skipped: set[str]  # confirmed by >=2 sources AND manifest has skipped_*
    extra_in_manifest: set[str]  # we downloaded, no source confirms
    single_source_candidates: set[str]  # 1 source only — human triage
    per_year_counts: dict[int, dict[str, int]]  # year -> {source -> count}
