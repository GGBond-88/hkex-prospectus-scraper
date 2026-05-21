"""Shared dataclasses and helpers for the L0 pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


def pad_ticker(raw: object) -> str:
    """Normalize an HKEX stock code to the 5-digit zero-padded string form.

    Accepts str or int. Strips surrounding whitespace from strings. Rejects
    empty, non-digit, oversized, negative, or None inputs with ValueError.
    """
    if raw is None:
        raise ValueError("ticker is None")
    if isinstance(raw, bool):  # bool is int subclass; reject explicitly
        raise ValueError(f"ticker must not be bool: {raw!r}")
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError(f"ticker must be non-negative: {raw!r}")
        s = str(raw)
    elif isinstance(raw, str):
        s = raw.strip()
    else:
        raise ValueError(f"ticker must be str or int, got {type(raw).__name__}")
    if not s or not s.isdigit() or len(s) > 5:
        raise ValueError(f"invalid ticker: {raw!r}")
    return s.zfill(5)


Market = Literal["MB", "GEM"]
Language = Literal["en", "zh", "bilingual"]
_VALID_MARKETS = {"MB", "GEM"}
_VALID_LANGUAGES = {"en", "zh", "bilingual"}


@dataclass(slots=True)
class Filing:
    """One discovered HKEX filing, pre-download."""
    hk_ticker: str
    doc_id: str
    doc_title: str
    doc_url: str
    doc_type: str
    market: Market
    language: Language
    is_final: bool
    publish_date: datetime
    company_name_en: str | None = None
    company_name_zh: str | None = None

    def __post_init__(self) -> None:
        self.hk_ticker = pad_ticker(self.hk_ticker)
        if self.market not in _VALID_MARKETS:
            raise ValueError(f"market must be one of {_VALID_MARKETS}, got {self.market!r}")
        if self.language not in _VALID_LANGUAGES:
            raise ValueError(f"language must be one of {_VALID_LANGUAGES}, got {self.language!r}")
        if self.publish_date.tzinfo is None:
            raise ValueError("publish_date must be timezone-aware")


class ManifestStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED_NO_ENGLISH = "skipped_no_english"
    SKIPPED_WRONG_DOC_TYPE = "skipped_wrong_doc_type"
    FAILED = "failed"
    PENDING = "pending"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(slots=True)
class ManifestEntry:
    """One row in data/raw_pdfs/manifest.json. Status-dependent fields are optional."""
    hk_ticker: str
    status: ManifestStatus
    # Identification
    doc_id: str | None = None
    doc_url: str | None = None
    doc_title: str | None = None
    company_name_en: str | None = None
    company_name_zh: str | None = None
    listing_date: str | None = None  # ISO date string from HKEX, kept verbatim
    market: str | None = None
    language: str | None = None
    # Success fields
    file_path: str | None = None
    file_sha256: str | None = None
    file_size_bytes: int | None = None
    downloaded_at: datetime | None = None
    # Skip fields
    skip_reason: str | None = None
    # Failure fields
    error: str | None = None
    first_attempted_at: datetime | None = None
    last_attempted_at: datetime | None = None
    attempt_count: int = 0
    # Always-present
    discovered_at: datetime | None = None

    def __post_init__(self) -> None:
        self.hk_ticker = pad_ticker(self.hk_ticker)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "hk_ticker": self.hk_ticker,
            "status": self.status.value,
        }
        for field_name in (
            "doc_id", "doc_url", "doc_title", "company_name_en", "company_name_zh",
            "listing_date", "market", "language", "file_path", "file_sha256",
            "file_size_bytes", "skip_reason", "error",
        ):
            v = getattr(self, field_name)
            if v is not None:
                d[field_name] = v
        for field_name in ("downloaded_at", "discovered_at", "first_attempted_at", "last_attempted_at"):
            v = _iso(getattr(self, field_name))
            if v is not None:
                d[field_name] = v
        if self.attempt_count:
            d["attempt_count"] = self.attempt_count
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ManifestEntry:
        raw_status = d.get("status")
        if not isinstance(raw_status, str):
            raise ValueError(f"status field missing or not str: {raw_status!r}")
        try:
            status = ManifestStatus(raw_status)
        except ValueError as e:
            raise ValueError(f"unknown manifest status: {raw_status!r}") from e
        return cls(
            hk_ticker=str(d["hk_ticker"]),
            status=status,
            doc_id=d.get("doc_id"),  # type: ignore[arg-type]
            doc_url=d.get("doc_url"),  # type: ignore[arg-type]
            doc_title=d.get("doc_title"),  # type: ignore[arg-type]
            company_name_en=d.get("company_name_en"),  # type: ignore[arg-type]
            company_name_zh=d.get("company_name_zh"),  # type: ignore[arg-type]
            listing_date=d.get("listing_date"),  # type: ignore[arg-type]
            market=d.get("market"),  # type: ignore[arg-type]
            language=d.get("language"),  # type: ignore[arg-type]
            file_path=d.get("file_path"),  # type: ignore[arg-type]
            file_sha256=d.get("file_sha256"),  # type: ignore[arg-type]
            file_size_bytes=d.get("file_size_bytes"),  # type: ignore[arg-type]
            downloaded_at=_parse_iso(d.get("downloaded_at")),  # type: ignore[arg-type]
            skip_reason=d.get("skip_reason"),  # type: ignore[arg-type]
            error=d.get("error"),  # type: ignore[arg-type]
            first_attempted_at=_parse_iso(d.get("first_attempted_at")),  # type: ignore[arg-type]
            last_attempted_at=_parse_iso(d.get("last_attempted_at")),  # type: ignore[arg-type]
            attempt_count=int(d.get("attempt_count", 0)),  # type: ignore[arg-type]
            discovered_at=_parse_iso(d.get("discovered_at")),  # type: ignore[arg-type]
        )


class DownloadOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class DownloadResult:
    hk_ticker: str
    outcome: DownloadOutcome
    file_path: str | None = None
    file_sha256: str | None = None
    file_size_bytes: int | None = None
    attempts: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        self.hk_ticker = pad_ticker(self.hk_ticker)

    @property
    def is_success(self) -> bool:
        return self.outcome == DownloadOutcome.SUCCESS
