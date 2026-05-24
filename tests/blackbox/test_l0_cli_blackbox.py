"""Black-box CLI tests for hk_ipo.l0.

These tests invoke the CLI as a subprocess and inspect stdout/stderr,
the manifest file, and the on-disk PDFs. They never import L0 internals.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.blackbox.stub_hkex_server import FIXTURES, StubState

pytestmark = pytest.mark.blackbox


# ---------- help / version ----------

def test_cli_help_lists_all_subcommands(run_cli) -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stderr
    for sub in ("backfill", "sync", "retry-failed", "refresh", "status", "verify"):
        assert sub in result.stdout, f"missing subcommand: {sub}"


# ---------- backfill / sync happy path ----------

def _arm_one_english_one_chinese(stub: StubState, sample_pdf: Path) -> None:
    # MVP-1: HKEX discovery now POSTs to titlesearch.xhtml and consumes HTML.
    # The JSON path is no longer hit by the production code; we leave the
    # JSON fixture armed too for any test that still depends on it.
    stub.html_response_path = FIXTURES / "discovery_window_2024_01.html"
    stub.json_response_path = FIXTURES / "discovery_window_2024_01.json"
    stub.pdf_response_path = sample_pdf


def test_backfill_dry_run_writes_nothing(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)

    result = run_cli(
        "backfill", "--since", "2024-01-01", "--until", "2024-01-31", "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert not manifest_path.exists(), "dry-run must not write manifest"
    assert list(pdfs_dir.glob("*.pdf")) == [], "dry-run must not write PDFs"
    assert "09999" in result.stdout, "should report discovered ticker"


def test_backfill_downloads_english_skips_chinese(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)

    result = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    assert result.returncode == 0, result.stderr
    assert (pdfs_dir / "09999.pdf").exists(), "English PDF must be saved"
    assert not (pdfs_dir / "09998.pdf").exists(), "Chinese-only must be skipped"
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["entries"]["09999"]["status"] == "success"
    assert manifest["entries"]["09999"]["file_path"] == "09999.pdf"
    assert manifest["entries"]["09999"]["file_size_bytes"] > 0
    assert len(manifest["entries"]["09999"]["file_sha256"]) == 64
    assert manifest["entries"]["09998"]["status"] == "skipped_no_english"


def test_backfill_idempotent_on_rerun(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)

    first = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert first.returncode == 0
    first_mtime = (pdfs_dir / "09999.pdf").stat().st_mtime_ns

    second = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert second.returncode == 0
    second_mtime = (pdfs_dir / "09999.pdf").stat().st_mtime_ns

    assert first_mtime == second_mtime, "second run must not rewrite the PDF"
    assert "already-have" in second.stdout.lower() or "skip" in second.stdout.lower()


def test_backfill_limit_caps_downloads(
    run_cli, stub_hkex, sample_pdf, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)

    result = run_cli(
        "backfill", "--since", "2024-01-01", "--until", "2024-01-31", "--limit", "0",
    )

    assert result.returncode == 0, result.stderr
    assert list(pdfs_dir.glob("*.pdf")) == []


# ---------- --limit N ----------

def test_backfill_limit_5_downloads_exactly_five_and_verifies_manifest(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir,
) -> None:
    """AC 2: backfill --since 2024-01-01 --limit 5 downloads exactly 5 PDFs,
    writes 5 entries to manifest, all status=success, all have non-zero sha256."""
    state, _ = stub_hkex
    state.html_response_path = FIXTURES / "discovery_window_2024_01_multi.html"
    state.json_response_path = FIXTURES / "discovery_window_2024_01_multi.json"
    state.pdf_response_path = sample_pdf

    result = run_cli(
        "backfill", "--since", "2024-01-01", "--until", "2024-01-31", "--limit", "5",
    )

    assert result.returncode == 0, result.stderr
    pdfs = sorted(pdfs_dir.glob("*.pdf"))
    assert len(pdfs) == 5, f"expected 5 PDFs, got {len(pdfs)}: {pdfs}"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    success_entries = {t: e for t, e in entries.items() if e["status"] == "success"}
    assert len(success_entries) == 5, \
        f"expected 5 success entries, got {len(success_entries)}: {list(success_entries)}"
    for ticker, entry in success_entries.items():
        assert len(entry["file_sha256"]) == 64, \
            f"{ticker}: expected 64-char sha256, got {entry['file_sha256']!r}"
        assert entry["file_size_bytes"] > 0, \
            f"{ticker}: expected positive file_size_bytes"


# ---------- HTML fallback (legacy / deprecated) ----------

@pytest.mark.skip(
    reason="MVP-1: JSON-then-HTML-fallback behavior was retired. The new "
    "discovery flow POSTs directly to titlesearch.xhtml, so this branch no "
    "longer exists. The discovery_html_fallback.html fixture is also keyed "
    "to the old <tr class='row'> table structure; not worth porting for v0.1."
)
def test_html_fallback_used_when_json_returns_500(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    state.json_status = 500
    state.html_response_path = FIXTURES / "discovery_html_fallback.html"
    state.pdf_response_path = sample_pdf

    result = run_cli("backfill", "--since", "2024-02-01", "--until", "2024-02-29")

    assert result.returncode == 0, result.stderr
    assert (pdfs_dir / "09997.pdf").exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"]["09997"]["status"] == "success"


# ---------- transient 503 retry ----------

def test_downloader_retries_transient_503(
    run_cli, stub_hkex, sample_pdf, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    state.pdf_fail_first_n = 2  # first two attempts 503, third succeeds

    result = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    assert result.returncode == 0, result.stderr
    assert (pdfs_dir / "09999.pdf").exists()
    assert state.pdf_failure_count == 2


# ---------- sync / retry-failed / refresh ----------

def test_sync_uses_last_incremental_sync(
    run_cli, stub_hkex, sample_pdf, manifest_path,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)

    first = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert first.returncode == 0
    after_first = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "last_incremental_sync" in after_first or "last_full_sync" in after_first

    # Arm empty for the next call; sync should still succeed with zero new.
    state.html_response_path = FIXTURES / "discovery_empty.html"
    state.json_response_path = FIXTURES / "discovery_empty.json"
    second = run_cli("sync")
    assert second.returncode == 0, second.stderr


def test_retry_failed_revisits_failed_entries(
    run_cli, stub_hkex, sample_pdf, manifest_path,
) -> None:
    state, _ = stub_hkex
    state.html_response_path = FIXTURES / "discovery_window_2024_01.html"
    state.json_response_path = FIXTURES / "discovery_window_2024_01.json"
    state.pdf_response_path = sample_pdf
    state.pdf_fail_first_n = 99  # exhaust download retries -> failed

    first = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert first.returncode == 0  # whole-run success, entry-level failed
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"]["09999"]["status"] == "failed"

    # Now allow downloads to succeed.
    state.pdf_fail_first_n = 0
    state.pdf_failure_count = 0
    second = run_cli("retry-failed")
    assert second.returncode == 0, second.stderr
    manifest2 = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest2["entries"]["09999"]["status"] == "success"


def test_refresh_overwrites_when_hash_differs(
    run_cli, stub_hkex, sample_pdf, manifest_path, pdfs_dir, tmp_path,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    first = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert first.returncode == 0
    orig_hash = json.loads(manifest_path.read_text(encoding="utf-8"))["entries"]["09999"]["file_sha256"]

    # Swap stub PDF for a different one.
    alt = tmp_path / "alt.pdf"
    alt.write_bytes(sample_pdf.read_bytes() + b"\n%EXTRA\n")
    state.pdf_response_path = alt

    second = run_cli("refresh", "09999")
    assert second.returncode == 0, second.stderr
    new_hash = json.loads(manifest_path.read_text(encoding="utf-8"))["entries"]["09999"]["file_sha256"]
    assert new_hash != orig_hash


# ---------- status / verify ----------

def test_status_reports_counts(run_cli, stub_hkex, sample_pdf) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    result = run_cli("status")
    assert result.returncode == 0, result.stderr
    assert "success" in result.stdout.lower()
    assert "1" in result.stdout  # one successful entry


def test_status_json_mode_emits_parseable_json(run_cli, stub_hkex, sample_pdf) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    result = run_cli("status", "--json")
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert "success" in parsed or "counts" in parsed


def test_verify_passes_on_intact_files(
    run_cli, stub_hkex, sample_pdf, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    result = run_cli("verify")
    assert result.returncode == 0, result.stderr


def test_verify_detects_hash_drift(
    run_cli, stub_hkex, sample_pdf, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")

    # Corrupt the on-disk PDF.
    target = pdfs_dir / "09999.pdf"
    target.write_bytes(target.read_bytes() + b"\nCORRUPTED")

    result = run_cli("verify")
    assert result.returncode != 0, "verify must exit non-zero on drift"
    assert "09999" in (result.stdout + result.stderr)


# ---------- crash safety ----------

def test_orphan_tmp_file_cleaned_on_startup(
    run_cli, stub_hkex, sample_pdf, pdfs_dir,
) -> None:
    state, _ = stub_hkex
    _arm_one_english_one_chinese(state, sample_pdf)
    orphan = pdfs_dir / "09999.pdf.tmp"
    orphan.write_bytes(b"leftover from a previous crashed run")

    result = run_cli("backfill", "--since", "2024-01-01", "--until", "2024-01-31")
    assert result.returncode == 0, result.stderr
    assert not orphan.exists(), "orphan .tmp must be swept"
    assert (pdfs_dir / "09999.pdf").exists()
