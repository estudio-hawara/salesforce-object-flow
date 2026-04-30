"""Application-wide logging configuration.

Logs go to the console at WARNING+ and to a rotating file under the
platform-specific user log directory at DEBUG+.
"""

import logging
import logging.handlers
from pathlib import Path

from platformdirs import PlatformDirs

_DIRS = PlatformDirs(appname="salesforce-object-flow", appauthor="hawara", roaming=False)
_LOG_DIR = Path(_DIRS.user_log_dir)
_LOG_FILE = _LOG_DIR / "salesforce-object-flow.log"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """Wire the root logger to console + rotating file handlers.

    Idempotent: a second call replaces existing handlers so reloads in a
    development shell don't accumulate duplicates.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def log_path() -> Path:
    """Return the resolved path to the active log file."""
    return _LOG_FILE
