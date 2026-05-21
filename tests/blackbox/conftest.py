"""Black-box test fixtures: stub HKEX server, isolated data dir, CLI runner."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.blackbox.stub_hkex_server import (
    FIXTURES,
    StubState,
    start_stub_server,
)


@pytest.fixture
def stub_hkex() -> Iterator[tuple[StubState, str]]:
    """Spin up the stub HKEX server for one test, tear it down after."""
    server, state, base_url = start_stub_server()
    try:
        yield state, base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def isolated_data(tmp_path: Path) -> Path:
    raw = tmp_path / "raw_pdfs"
    raw.mkdir()
    (tmp_path / "logs" / "l0").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def run_cli(isolated_data: Path, stub_hkex: tuple[StubState, str]):
    """Return a callable that runs `python -m hk_ipo.l0 ...` with isolated env."""
    _, base_url = stub_hkex

    def _run(*args: str, extra_env: dict[str, str] | None = None,
             timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HKEX_JSON_API_BASE"] = f"{base_url}/search/titlesearchservlet.do"
        env["HKEX_HTML_SEARCH_BASE"] = f"{base_url}/search/titlesearch.xhtml"
        env["HKEX_PDF_BASE"] = f"{base_url}"
        env["HK_IPO_DATA_DIR"] = str(isolated_data)
        env["HKEX_CONTACT_EMAIL"] = "test@example.com"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "hk_ipo.l0", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    return _run


@pytest.fixture
def manifest_path(isolated_data: Path) -> Path:
    return isolated_data / "raw_pdfs" / "manifest.json"


@pytest.fixture
def pdfs_dir(isolated_data: Path) -> Path:
    return isolated_data / "raw_pdfs"


@pytest.fixture
def sample_pdf() -> Path:
    p = FIXTURES / "sample_prospectus.pdf"
    assert p.exists(), "Task 002 step 1 missing"
    return p
