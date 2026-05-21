"""Unit tests for hk_ipo.config."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hk_ipo.config import Config, build_user_agent


def test_config_default_paths_anchored_at_repo() -> None:
    cfg = Config.from_env(env={})
    assert cfg.data_dir.name == "data"
    assert cfg.raw_pdfs_dir == cfg.data_dir / "raw_pdfs"
    assert cfg.manifest_path == cfg.raw_pdfs_dir / "manifest.json"
    assert cfg.log_dir == cfg.data_dir / "logs" / "l0"


def test_config_honors_hk_ipo_data_dir_env(tmp_path: Path) -> None:
    cfg = Config.from_env(env={"HK_IPO_DATA_DIR": str(tmp_path)})
    assert cfg.data_dir == tmp_path
    assert cfg.raw_pdfs_dir == tmp_path / "raw_pdfs"
    assert cfg.manifest_path == tmp_path / "raw_pdfs" / "manifest.json"


def test_config_default_endpoints() -> None:
    cfg = Config.from_env(env={})
    assert cfg.json_api_base.startswith("https://www1.hkexnews.hk/")
    assert cfg.html_search_base.startswith("https://www1.hkexnews.hk/")


def test_config_endpoints_overridable() -> None:
    cfg = Config.from_env(env={
        "HKEX_JSON_API_BASE": "http://localhost:9999/json",
        "HKEX_HTML_SEARCH_BASE": "http://localhost:9999/html",
        "HKEX_PDF_BASE": "http://localhost:9999",
    })
    assert cfg.json_api_base == "http://localhost:9999/json"
    assert cfg.html_search_base == "http://localhost:9999/html"
    assert cfg.pdf_base == "http://localhost:9999"


def test_config_pdf_base_defaults_to_hkex() -> None:
    cfg = Config.from_env(env={})
    assert cfg.pdf_base.startswith("https://www1.hkexnews.hk")


def test_config_workers_default_and_override() -> None:
    assert Config.from_env(env={}).default_workers == 4
    assert Config.from_env(env={"L0_WORKERS": "8"}).default_workers == 8


def test_config_rejects_non_int_workers() -> None:
    with pytest.raises(ValueError):
        Config.from_env(env={"L0_WORKERS": "many"})


def test_config_backfill_start_date_constant() -> None:
    assert Config.from_env(env={}).backfill_start_date == "2010-01-01"


# ---------- build_user_agent ----------

def test_user_agent_includes_email_when_set() -> None:
    ua = build_user_agent("ops@example.com")
    assert ua == "hk-ipo-research/0.1 (research; +ops@example.com)"


def test_user_agent_omits_email_when_empty() -> None:
    # SI-006: avoid the dangling "+<empty>" suffix.
    ua = build_user_agent("")
    assert ua == "hk-ipo-research/0.1 (research)"
    assert "+" not in ua


def test_user_agent_version_override() -> None:
    ua = build_user_agent("x@y", version="0.2")
    assert ua.startswith("hk-ipo-research/0.2 ")
