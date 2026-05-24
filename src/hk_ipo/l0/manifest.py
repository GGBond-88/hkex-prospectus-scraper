"""JSON manifest store at data/raw_pdfs/manifest.json. File-locked, atomic."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portalocker

logger = logging.getLogger("hk_ipo")

from hk_ipo.l0.models import Filing, ManifestEntry, ManifestStatus, pad_ticker

SCHEMA_VERSION = 1


class ManifestStore:
    """In-memory cache of manifest.json, with explicit save() to flush."""

    def __init__(
        self,
        path: Path,
        entries: dict[str, ManifestEntry],
        schema_version: int = SCHEMA_VERSION,
        last_full_sync: datetime | None = None,
        last_incremental_sync: datetime | None = None,
    ) -> None:
        self.path = path
        self._entries = entries
        self.schema_version = schema_version
        self.last_full_sync = last_full_sync
        self.last_incremental_sync = last_incremental_sync

    # ---- load / save ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "ManifestStore":
        if not path.exists():
            return cls(path=path, entries={})
        # Acquire lock on .lock sidecar so writers' os.replace does not conflict.
        with portalocker.Lock(str(path) + ".lock", timeout=10):
            raw = json.loads(path.read_text(encoding="utf-8"))
        cls._validate_schema(raw)
        entries: dict[str, ManifestEntry] = {}
        for k, v in raw.get("entries", {}).items():
            entry = ManifestEntry.from_dict(v)
            entries[pad_ticker(k)] = entry
        return cls(
            path=path,
            entries=entries,
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            last_full_sync=_parse_iso(raw.get("last_full_sync")),
            last_incremental_sync=_parse_iso(raw.get("last_incremental_sync")),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "entries": {k: v.to_dict() for k, v in sorted(self._entries.items())},
        }
        if self.last_full_sync:
            payload["last_full_sync"] = _iso(self.last_full_sync)
        if self.last_incremental_sync:
            payload["last_incremental_sync"] = _iso(self.last_incremental_sync)

        # Atomic write through a tmp file in the same directory.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".manifest.", suffix=".json.tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            # Lock the target during replace to serialize writers.
            with portalocker.Lock(str(self.path) + ".lock", timeout=10):
                _safe_replace(tmp_name, str(self.path), max_retries=10, base_delay=0.2)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate_schema(raw: dict[str, Any]) -> None:
        v = raw.get("schema_version")
        if v is None:
            raise ValueError("manifest missing schema_version")
        if int(v) > SCHEMA_VERSION:
            raise ValueError(f"manifest schema_version {v} newer than supported {SCHEMA_VERSION}")

    # ---- query / mutate (placeholders, expanded in later steps) --------------

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def get(self, ticker: str) -> ManifestEntry:
        return self._entries[pad_ticker(ticker)]

    def upsert(self, entry: ManifestEntry) -> None:
        self._entries[entry.hk_ticker] = entry

    def add_success(
        self, *, hk_ticker: str, doc_id: str, doc_url: str,
        file_path: str, file_sha256: str, file_size_bytes: int,
        discovered_at: datetime,
        doc_title: str | None = None,
        company_name_en: str | None = None,
        company_name_zh: str | None = None,
        listing_date: str | None = None,
        market: str | None = None,
        language: str | None = None,
    ) -> None:
        self.upsert(ManifestEntry(
            hk_ticker=hk_ticker,
            status=ManifestStatus.SUCCESS,
            doc_id=doc_id, doc_url=doc_url, doc_title=doc_title,
            company_name_en=company_name_en, company_name_zh=company_name_zh,
            listing_date=listing_date, market=market, language=language,
            file_path=file_path, file_sha256=file_sha256,
            file_size_bytes=file_size_bytes,
            downloaded_at=datetime.now(tz=timezone.utc),
            discovered_at=discovered_at,
        ))

    def mark_failed(
        self, *, hk_ticker: str, doc_url: str, error: str,
        discovered_at: datetime,
    ) -> None:
        ticker = pad_ticker(hk_ticker)
        now = datetime.now(tz=timezone.utc)
        existing = self._entries.get(ticker)
        if existing is not None and existing.status == ManifestStatus.FAILED:
            existing.last_attempted_at = now
            existing.attempt_count += 1
            existing.error = error
            return
        self.upsert(ManifestEntry(
            hk_ticker=ticker,
            status=ManifestStatus.FAILED,
            doc_url=doc_url,
            error=error,
            first_attempted_at=now,
            last_attempted_at=now,
            attempt_count=1,
            discovered_at=discovered_at,
        ))

    def mark_skipped_no_english(
        self, *, hk_ticker: str, doc_id: str, doc_url: str,
        discovered_at: datetime,
        company_name_zh: str | None = None,
        company_name_en: str | None = None,
    ) -> None:
        self.upsert(ManifestEntry(
            hk_ticker=hk_ticker,
            status=ManifestStatus.SKIPPED_NO_ENGLISH,
            doc_id=doc_id, doc_url=doc_url,
            company_name_zh=company_name_zh,
            company_name_en=company_name_en,
            discovered_at=discovered_at,
            skip_reason="no_english_version_published",
        ))

    def mark_skipped_wrong_doc_type(
        self, *, hk_ticker: str, doc_id: str, doc_url: str,
        discovered_at: datetime,
    ) -> None:
        self.upsert(ManifestEntry(
            hk_ticker=hk_ticker,
            status=ManifestStatus.SKIPPED_WRONG_DOC_TYPE,
            doc_id=doc_id, doc_url=doc_url,
            discovered_at=discovered_at,
            skip_reason="not_final_prospectus",
        ))

    def get_status(self, ticker: str) -> ManifestStatus | None:
        e = self._entries.get(pad_ticker(ticker))
        return e.status if e else None

    def failed_entries(self, *, max_age_days: int | None = None) -> Iterable[ManifestEntry]:
        cutoff = None
        if max_age_days is not None:
            from datetime import timedelta
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
        for e in self._entries.values():
            if e.status != ManifestStatus.FAILED:
                continue
            if cutoff is not None and (e.last_attempted_at is None or e.last_attempted_at < cutoff):
                continue
            yield e

    def iter_tickers(self) -> Iterable[str]:
        return iter(self._entries.keys())

    def pending_filings(self, filings: Iterable["Filing"]) -> Iterable["Filing"]:
        """Yield filings that should be downloaded (no prior success/skip)."""
        for f in filings:
            existing = self._entries.get(f.hk_ticker)
            if existing is None:
                yield f
                continue
            if existing.status in (
                ManifestStatus.SUCCESS,
                ManifestStatus.SKIPPED_NO_ENGLISH,
                ManifestStatus.SKIPPED_WRONG_DOC_TYPE,
                ManifestStatus.FAILED,  # sync skips failed; retry-failed uses different selector
            ):
                continue
            yield f  # pending / unexpected -> treat as new


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _safe_replace(src: str | Path, dst: str | Path, max_retries: int = 5, base_delay: float = 0.1) -> None:
    """Atomically replace dst with src, retrying on Windows file lock errors.

    Windows antivirus and other processes may lock a file briefly after write,
    causing PermissionError on os.replace(). Falls back to copy+unlink on
    exhaustion.
    """
    import shutil

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                if delay > 5.0:
                    delay = 5.0
                logger.debug(
                    "os.replace(%s, %s) failed (attempt %d/%d): %s, retrying in %.2fs",
                    src, dst, attempt + 1, max_retries, e, delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "os.replace(%s, %s) failed after %d attempts, trying copy+unlink",
                    src, dst, max_retries,
                )
    try:
        shutil.copy2(src, dst)
    except Exception:
        if last_error:
            raise last_error
        raise
    try:
        os.unlink(src)
    except Exception:
        pass
