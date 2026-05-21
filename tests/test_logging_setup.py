"""Unit tests for hk_ipo.logging_setup."""
from __future__ import annotations

import logging
from pathlib import Path

from hk_ipo.logging_setup import configure_run_logger


def test_configure_run_logger_creates_log_file(tmp_path: Path) -> None:
    logger = configure_run_logger(tmp_path, run_id="testrun", level="DEBUG")
    logger.info("hello-world")
    for h in logger.handlers:
        h.flush()
    log_files = list(tmp_path.glob("run-*.log"))
    assert len(log_files) == 1
    assert "hello-world" in log_files[0].read_text(encoding="utf-8")


def test_configure_run_logger_level(tmp_path: Path) -> None:
    logger = configure_run_logger(tmp_path, run_id="x", level="WARNING")
    assert logger.level == logging.WARNING


def test_configure_run_logger_does_not_duplicate_handlers(tmp_path: Path) -> None:
    a = configure_run_logger(tmp_path, run_id="first", level="INFO")
    b = configure_run_logger(tmp_path, run_id="second", level="INFO")
    assert a is b
    # Two run files because run_id differs:
    assert len(list(tmp_path.glob("run-*.log"))) == 2
    # But the logger only carries one file handler per active run_id (most recent wins):
    file_handlers = [h for h in b.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
