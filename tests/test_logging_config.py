import logging

from app.core.logging_config import (
    PollingAccessLogFilter,
    _POLL_ACCESS_FILTER_MARKER,
    _configure_http_client_logging,
    _configure_uvicorn_access_logging,
)


def _access_record(args=(), msg='') -> logging.LogRecord:
    return logging.LogRecord(
        name='uvicorn.access',
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_polling_access_filter_suppresses_successful_live_poll_records():
    access_filter = PollingAccessLogFilter()
    record = _access_record(args=('127.0.0.1:12345', 'GET', '/api/download-jobs?x=1', '1.1', 200))

    assert access_filter.filter(record) is False


def test_polling_access_filter_keeps_poll_errors():
    access_filter = PollingAccessLogFilter()
    record = _access_record(args=('127.0.0.1:12345', 'GET', '/api/download-jobs', '1.1', 500))

    assert access_filter.filter(record) is True


def test_polling_access_filter_keeps_non_poll_requests():
    access_filter = PollingAccessLogFilter()
    record = _access_record(args=('127.0.0.1:12345', 'GET', '/api/download-jobs/7', '1.1', 200))

    assert access_filter.filter(record) is True


def test_polling_access_filter_supports_formatted_uvicorn_messages():
    access_filter = PollingAccessLogFilter()
    record = _access_record(msg='127.0.0.1:12345 - "GET /api/metrics HTTP/1.1" 200')

    assert access_filter.filter(record) is False


def test_polling_access_filter_env_switch(monkeypatch):
    access_logger = logging.getLogger('uvicorn.access')
    original_filters = list(access_logger.filters)
    try:
        access_logger.filters = []
        monkeypatch.delenv('OPTIMIZARR_SUPPRESS_POLL_ACCESS_LOGS', raising=False)
        _configure_uvicorn_access_logging()
        assert any(getattr(existing_filter, _POLL_ACCESS_FILTER_MARKER, False) for existing_filter in access_logger.filters)

        monkeypatch.setenv('OPTIMIZARR_SUPPRESS_POLL_ACCESS_LOGS', '0')
        _configure_uvicorn_access_logging()
        assert not any(getattr(existing_filter, _POLL_ACCESS_FILTER_MARKER, False) for existing_filter in access_logger.filters)
    finally:
        access_logger.filters = original_filters


def test_http_client_info_logs_are_suppressed_by_default(monkeypatch):
    httpx_logger = logging.getLogger('httpx')
    httpcore_logger = logging.getLogger('httpcore')
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    try:
        monkeypatch.delenv('OPTIMIZARR_SUPPRESS_HTTPX_INFO_LOGS', raising=False)
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)

        _configure_http_client_logging()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)


def test_http_client_info_logs_env_switch(monkeypatch):
    httpx_logger = logging.getLogger('httpx')
    httpcore_logger = logging.getLogger('httpcore')
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    try:
        monkeypatch.setenv('OPTIMIZARR_SUPPRESS_HTTPX_INFO_LOGS', '0')
        httpx_logger.setLevel(logging.WARNING)
        httpcore_logger.setLevel(logging.WARNING)

        _configure_http_client_logging()

        assert httpx_logger.level == logging.NOTSET
        assert httpcore_logger.level == logging.NOTSET
    finally:
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)
