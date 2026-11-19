"""SmartQueue application logging: console plus a rotating log file.

Level comes from SMARTQUEUE_LOG_LEVEL (default INFO). The log file comes
from SMARTQUEUE_LOG_FILE, or defaults to ``logs/smartqueue.log`` next to
the data directory. Configuration is idempotent: calling it once per app
instance never stacks duplicate handlers.

Secrets are never logged: no passwords, tokens, session data, or database
connection strings — only event names and record IDs.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "smartqueue"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 5

_configured_files = set()
_console_configured = False


def _resolve_level(value):
    if isinstance(value, int):
        return value
    level = logging.getLevelName(str(value or "INFO").upper())
    return level if isinstance(level, int) else logging.INFO


def configure_logging(data_dir=None, log_file=None, level=None):
    """Configure the ``smartqueue`` logger and return it."""
    global _console_configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_resolve_level(level or os.environ.get("SMARTQUEUE_LOG_LEVEL")))
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if not _console_configured:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        _console_configured = True

    path = log_file or os.environ.get("SMARTQUEUE_LOG_FILE")
    if not path and data_dir:
        path = Path(data_dir).parent / "logs" / "smartqueue.log"
    if path:
        key = os.path.abspath(str(path))
        if key not in _configured_files:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
                )
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                _configured_files.add(key)
            except OSError:
                logger.warning("Could not open log file %s; logging to console only.", path)
    return logger
