import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = '/config/logs'
DEFAULT_LOG_LEVEL = 'INFO'
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_LOG_BACKUP_COUNT = 10
HTTP_CLIENT_LOGGERS = ('httpx', 'httpcore')
QUIET_POLL_ACCESS_PATHS = frozenset({
    '/api/download-jobs',
    '/api/jobs',
    '/api/metrics',
    '/api/queue/status',
    '/download-jobs',
    '/jobs',
    '/metrics',
    '/queue/status',
})
_ACCESS_MESSAGE_RE = re.compile(r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[^"]+"\s+(?P<status>\d{3})')
_POLL_ACCESS_FILTER_MARKER = '_optimizarr_poll_access_filter'


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    return default


class PollingAccessLogFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        setattr(self, _POLL_ACCESS_FILTER_MARKER, True)

    def filter(self, record: logging.LogRecord) -> bool:
        parsed = self._parse_record(record)
        if parsed is None:
            return True
        method, path, status_code = parsed
        normalized_path = path.split('?', 1)[0]
        if method in {'GET', 'HEAD'} and normalized_path in QUIET_POLL_ACCESS_PATHS and 200 <= status_code < 400:
            return False
        return True

    @staticmethod
    def _parse_record(record: logging.LogRecord) -> tuple[str, str, int] | None:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            try:
                method = str(args[1]).upper()
                path = str(args[2])
                status_code = int(args[4])
                return method, path, status_code
            except (TypeError, ValueError):
                return None

        message = record.getMessage()
        match = _ACCESS_MESSAGE_RE.search(message)
        if not match:
            return None
        try:
            status_code = int(match.group('status'))
        except ValueError:
            return None
        return match.group('method').upper(), match.group('path'), status_code


def _configure_uvicorn_access_logging() -> None:
    access_logger = logging.getLogger('uvicorn.access')
    existing_filters = [
        existing_filter
        for existing_filter in access_logger.filters
        if not getattr(existing_filter, _POLL_ACCESS_FILTER_MARKER, False)
    ]
    access_logger.filters = existing_filters

    if _parse_bool_env('OPTIMIZARR_SUPPRESS_POLL_ACCESS_LOGS', True):
        access_logger.addFilter(PollingAccessLogFilter())


def _configure_http_client_logging() -> None:
    level = logging.WARNING if _parse_bool_env('OPTIMIZARR_SUPPRESS_HTTPX_INFO_LOGS', True) else logging.NOTSET
    for logger_name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(logger_name).setLevel(level)


def configure_logging() -> None:
    log_dir = Path(os.getenv('OPTIMIZARR_LOG_DIR', DEFAULT_LOG_DIR))
    log_file = log_dir / 'optimizarr.log'
    log_level_name = os.getenv('LOG_LEVEL', DEFAULT_LOG_LEVEL).upper()
    max_bytes = _parse_positive_int_env('OPTIMIZARR_LOG_MAX_BYTES', DEFAULT_LOG_MAX_BYTES)
    backup_count = _parse_positive_int_env('OPTIMIZARR_LOG_BACKUP_COUNT', DEFAULT_LOG_BACKUP_COUNT)

    log_dir.mkdir(parents=True, exist_ok=True)
    _configure_uvicorn_access_logging()
    _configure_http_client_logging()

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
