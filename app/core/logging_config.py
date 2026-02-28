import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = '/config/logs'
DEFAULT_LOG_LEVEL = 'INFO'
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_LOG_BACKUP_COUNT = 14


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def configure_logging() -> None:
    log_dir = Path(os.getenv('OPTIMIZARR_LOG_DIR', DEFAULT_LOG_DIR))
    log_file = log_dir / 'optimizarr.log'
    log_level_name = os.getenv('LOG_LEVEL', DEFAULT_LOG_LEVEL).upper()
    max_bytes = _parse_positive_int_env('OPTIMIZARR_LOG_MAX_BYTES', DEFAULT_LOG_MAX_BYTES)
    backup_count = _parse_positive_int_env('OPTIMIZARR_LOG_BACKUP_COUNT', DEFAULT_LOG_BACKUP_COUNT)

    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid adding handlers more than once (e.g. during hot-reload)
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # Rotate when a file reaches max_bytes and keep backup_count historical files.
    # Total disk use is bounded to roughly max_bytes * (backup_count + 1).
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
        delay=True,
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler so 'docker logs' / stdout captures everything
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
