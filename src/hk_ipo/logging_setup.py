"""Per-run logger for the L0 pipeline. One file per CLI invocation."""
from __future__ import annotations

import logging
from pathlib import Path

_LOGGER_NAME = "hk_ipo"


def configure_run_logger(log_dir: Path, *, run_id: str, level: str = "INFO") -> logging.Logger:
    """Configure (or reconfigure) the shared hk_ipo logger to write to a fresh run file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{run_id}.log"

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    # Drop any existing file handler so re-configuration targets the new run.
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            logger.removeHandler(h)
            h.close()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger
