"""Reads manifest.json and converts entries to NormalizedEntry for L1 reporting."""
from __future__ import annotations

import json
import re
from pathlib import Path

from hk_ipo.l0.manifest import ManifestStore
from hk_ipo.l1.models import NormalizedEntry

# Matches /listconews/(sehk|gem)/YYYY/MMDD/ in doc URLs.
_URL_PATTERN = re.compile(r"/listconews/(?:sehk|gem)/(\d{4})/(\d{2})(\d{2})/")


def _parse_year_month(doc_url: str | None) -> tuple[int, int]:
    """Extract (year, month) from a doc_url.

    Handles both:
      - /listedco/listconews/sehk/YYYY/MMDD/...  (Main Board)
      - /listedco/listconews/gem/YYYY/MMDD/...   (GEM)

    Returns (0, 0) when doc_url is None or does not match.
    """
    if doc_url is None:
        return 0, 0
    m = _URL_PATTERN.search(doc_url)
    if m is None:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def read_manifest(manifest_path: Path) -> list[NormalizedEntry]:
    """Read manifest.json and return a list of NormalizedEntry.

    Uses ManifestStore.load() to parse the manifest, then converts each
    ManifestEntry to a NormalizedEntry, extracting year/month from doc_url.
    """
    store = ManifestStore.load(manifest_path)
    result: list[NormalizedEntry] = []

    for ticker, entry in sorted(store._entries.items()):
        year, month = _parse_year_month(entry.doc_url)

        result.append(
            NormalizedEntry(
                hk_ticker=ticker,
                status=entry.status.value,
                year=year,
                month=month,
                company_name_en=entry.company_name_en,
                doc_url=entry.doc_url,
                file_path=entry.file_path,
            )
        )

    return result


def write_normalized(entries: list[NormalizedEntry], path: Path) -> None:
    """Write a list of NormalizedEntry to a JSON file.

    Creates parent directories as needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    serialized = [
        {
            "hk_ticker": e.hk_ticker,
            "status": e.status,
            "year": e.year,
            "month": e.month,
            "company_name_en": e.company_name_en,
            "doc_url": e.doc_url,
            "file_path": e.file_path,
        }
        for e in entries
    ]

    path.write_text(
        json.dumps(serialized, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
