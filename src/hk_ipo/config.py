"""Configuration constants and env loading for hk_ipo."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _repo_root() -> Path:
    """Walk up from this file looking for pyproject.toml; fall back to cwd."""
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


@dataclass(slots=True, frozen=True)
class Config:
    data_dir: Path
    raw_pdfs_dir: Path
    manifest_path: Path
    log_dir: Path
    json_api_base: str            # legacy / deprecated (kept for backward-compat tests)
    html_search_base: str         # primary search endpoint (POST titlesearch.xhtml)
    partial_lookup_base: str      # ticker -> stockId JSONP autocomplete (partial.do)
    pdf_base: str
    contact_email: str
    default_workers: int
    backfill_start_date: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = env if env is not None else os.environ
        data_dir = Path(env.get("HK_IPO_DATA_DIR") or (_repo_root() / "data"))
        raw_pdfs_dir = data_dir / "raw_pdfs"
        workers_raw = env.get("L0_WORKERS", "4")
        try:
            workers = int(workers_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"L0_WORKERS must be int, got {workers_raw!r}") from e
        if workers < 1:
            raise ValueError(f"L0_WORKERS must be >=1, got {workers}")
        return cls(
            data_dir=data_dir,
            raw_pdfs_dir=raw_pdfs_dir,
            manifest_path=raw_pdfs_dir / "manifest.json",
            log_dir=data_dir / "logs" / "l0",
            json_api_base=env.get(
                "HKEX_JSON_API_BASE",
                "https://www1.hkexnews.hk/search/titlesearchservlet.do",
            ),
            html_search_base=env.get(
                "HKEX_HTML_SEARCH_BASE",
                "https://www1.hkexnews.hk/search/titlesearch.xhtml",
            ),
            partial_lookup_base=env.get(
                "HKEX_PARTIAL_LOOKUP_BASE",
                "https://www1.hkexnews.hk/search/partial.do",
            ),
            pdf_base=env.get(
                "HKEX_PDF_BASE",
                "https://www1.hkexnews.hk",
            ),
            contact_email=env.get("HKEX_CONTACT_EMAIL", ""),
            default_workers=workers,
            backfill_start_date="2010-01-01",
        )


def build_user_agent(contact_email: str, *, version: str = "0.1") -> str:
    """Spec section 7 user-agent, with SI-006 handling for empty email."""
    base = f"hk-ipo-research/{version}"
    if contact_email:
        return f"{base} (research; +{contact_email})"
    return f"{base} (research)"
