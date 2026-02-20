import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.getenv('OPTIMIZARR_LOG_DIR', '/config/logs'))
LOG_FILE = LOG_DIR / 'optimizarr.log'

# Configurable via LOG_LEVEL env var (DEBUG, INFO, WARNING, ERROR, CRITICAL)
_LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, _LOG_LEVEL, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid adding handlers more than once (e.g. during hot-reload)
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # Rotate at midnight UTC, keep 14 days; files are suffixed YYYY-MM-DD
    file_handler = TimedRotatingFileHandler(
        LOG_FILE,
        when='midnight',
        interval=1,
        backupCount=14,
        encoding='utf-8',
        utc=True,
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler so 'docker logs' / stdout captures everything
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
