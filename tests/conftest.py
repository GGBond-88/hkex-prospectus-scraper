"""Shared pytest fixtures for the hk_ipo test suite."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide an isolated data/ tree mirroring the production layout."""
    raw = tmp_path / "raw_pdfs"
    raw.mkdir()
    logs = tmp_path / "logs" / "l0"
    logs.mkdir(parents=True)
    return tmp_path
