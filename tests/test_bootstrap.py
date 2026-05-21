"""Smoke test that the bootstrap layout is importable."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_hk_ipo_package_importable() -> None:
    import hk_ipo
    assert hk_ipo.__doc__ is not None


def test_l0_subpackage_importable() -> None:
    import hk_ipo.l0
    assert hk_ipo.l0.__doc__ is not None


def test_gitkeep_files_not_ignored_by_git() -> None:
    """Verify that .gitkeep placeholders in data/ dirs are tracked by git."""
    repo_root = Path(__file__).resolve().parent.parent
    gitkeep_paths = [
        "data/raw_pdfs/.gitkeep",
        "data/logs/l0/.gitkeep",
    ]
    git_exe = shutil.which("git")
    assert git_exe is not None, "git CLI not found on PATH"

    for path in gitkeep_paths:
        result = subprocess.run(
            [git_exe, "check-ignore", path],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        assert result.returncode != 0, (
            f"{path} should not be ignored by git, "
            f"but it is (matched pattern: {result.stdout.strip()})"
        )
