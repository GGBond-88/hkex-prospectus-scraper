"""Black-box CLI tests for L1 subcommands (report, validate, report-all)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.blackbox


# ---------------------------------------------------------------------------
# report --help
# ---------------------------------------------------------------------------


def test_report_help(run_cli) -> None:
    """python -m hk_ipo.l0 report --help should succeed and show help."""
    result = run_cli("report", "--help")
    assert result.returncode == 0, result.stderr
    assert "report" in result.stdout


def test_validate_help(run_cli) -> None:
    """python -m hk_ipo.l0 validate --help should succeed and show help."""
    result = run_cli("validate", "--help")
    assert result.returncode == 0, result.stderr
    assert "validate" in result.stdout


def test_report_all_help(run_cli) -> None:
    """python -m hk_ipo.l0 report-all --help should succeed and show help."""
    result = run_cli("report-all", "--help")
    assert result.returncode == 0, result.stderr
    assert "report-all" in result.stdout


# ---------------------------------------------------------------------------
# report against real manifest
# ---------------------------------------------------------------------------


def test_report_against_real_manifest(run_cli, isolated_data: Path) -> None:
    """Run report against sample_manifest fixture and verify summary.md is written."""
    fixture = Path(__file__).parent.parent / "fixtures" / "sample_manifest.json"
    output = isolated_data / "summary.md"

    result = run_cli(
        "report",
        "--manifest", str(fixture),
        "--output", str(output),
    )

    # Report should succeed even with a limited sample manifest
    assert result.returncode == 0, result.stderr
    assert output.exists(), f"Expected {output} to exist"
    text = output.read_text(encoding="utf-8")
    assert "# HKEX IPO Prospectus Download Summary" in text
    assert "01810" in text or "Xiaomi" in text


# ---------------------------------------------------------------------------
# help listing includes L1 subcommands
# ---------------------------------------------------------------------------


def test_top_level_help_includes_l1_commands(run_cli) -> None:
    """python -m hk_ipo.l0 --help should list report, validate, report-all."""
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    for cmd in ("report", "validate", "report-all"):
        assert cmd in result.stdout, f"Missing L1 subcommand: {cmd}"
