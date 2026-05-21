"""End-to-end smoke: hits live HKEX. Run with `pytest -m e2e`. Skipped by default."""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def isolated(tmp_path: Path) -> Path:
    (tmp_path / "raw_pdfs").mkdir()
    (tmp_path / "logs" / "l0").mkdir(parents=True)
    return tmp_path


def test_live_backfill_dry_run_recent_window_returns_filings(isolated: Path) -> None:
    """AC 1 (partial): discovery against the real HKEX returns >=1 hit for a recent month."""
    import os
    env = os.environ.copy()
    env["HK_IPO_DATA_DIR"] = str(isolated)
    end = date.today()
    start = end - timedelta(days=30)
    result = subprocess.run(
        [
            sys.executable, "-m", "hk_ipo.l0", "backfill",
            "--since", start.isoformat(),
            "--until", end.isoformat(),
            "--dry-run",
        ],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    # The exact count depends on the current pipeline; assert non-empty discovery.
    assert "Discovered:" in result.stdout
    lines = [l for l in result.stdout.splitlines() if "Discovered:" in l]
    assert lines
    n = int(lines[0].split(":")[1].strip().split()[0])
    assert n >= 1, f"expected >=1 filing discovered in last 30 days, got {n}"


@pytest.mark.slow
def test_live_backfill_h1_2024_meets_30_filings_threshold(isolated: Path) -> None:
    """AC 1 (full): 2024 H1 must surface >=30 English final prospectuses."""
    import os
    env = os.environ.copy()
    env["HK_IPO_DATA_DIR"] = str(isolated)
    result = subprocess.run(
        [
            sys.executable, "-m", "hk_ipo.l0", "backfill",
            "--since", "2024-01-01", "--until", "2024-06-30", "--dry-run",
        ],
        env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    n_lines = [l for l in result.stdout.splitlines() if l.startswith("DRY-RUN")]
    assert len(n_lines) >= 30, \
        f"AC 1 threshold not met: {len(n_lines)} discovered (need >=30)"
