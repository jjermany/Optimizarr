from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

BOOTSTRAP_TOKEN_HEADER_NAME = 'x-setup-token'
_BOOTSTRAP_TOKEN: str | None = None
_BOOTSTRAP_TOKEN_LOGGED = False


def media_root() -> Path:
    raw = os.getenv('MEDIA_ROOT', '/data/media').strip() or '/data/media'
    return Path(raw).resolve()


def workspace_root_base() -> Path:
    raw = os.getenv('OPTIMIZARR_WORKSPACE_ROOT_BASE', '/cache').strip() or '/cache'
    base = Path(raw).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_workspace_root() -> Path:
    return workspace_root_base() / 'workspaces'


def get_bootstrap_token() -> str:
    global _BOOTSTRAP_TOKEN
    global _BOOTSTRAP_TOKEN_LOGGED

    configured = (os.getenv('OPTIMIZARR_BOOTSTRAP_TOKEN') or '').strip()
    if configured:
        return configured

    if _BOOTSTRAP_TOKEN is None:
        _BOOTSTRAP_TOKEN = secrets.token_urlsafe(24)

    if not _BOOTSTRAP_TOKEN_LOGGED:
        logger.warning(
            'No OPTIMIZARR_BOOTSTRAP_TOKEN configured. Generated one-time setup token: %s',
            _BOOTSTRAP_TOKEN,
        )
        _BOOTSTRAP_TOKEN_LOGGED = True
    return _BOOTSTRAP_TOKEN


def is_safe_same_origin(origin: str, expected_origin: str) -> bool:
    origin_value = origin.strip().rstrip('/')
    expected_value = expected_origin.strip().rstrip('/')
    return bool(origin_value) and origin_value == expected_value


def normalize_path_within_root(
    value: str,
    *,
    root: Path,
    must_exist: bool,
    must_be_dir: bool,
) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError('path must be an absolute path')

    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise ValueError('path must exist') from exc
    if must_exist and not resolved.exists():
        raise ValueError('path must exist')
    if must_be_dir and must_exist and not resolved.is_dir():
        raise ValueError('path must be a directory')

    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f'path must be within {root_resolved}') from exc

    return str(resolved)


def normalize_workspace_root(value: str) -> str:
    return normalize_path_within_root(
        value,
        root=workspace_root_base(),
        must_exist=False,
        must_be_dir=False,
    )


def coerce_workspace_root(value: str | None) -> str:
    candidate = (value or '').strip()
    if candidate:
        try:
            return normalize_workspace_root(candidate)
        except ValueError:
            logger.warning(
                'Configured workspace_root %r is outside the allowed base %s; falling back to %s',
                candidate,
                workspace_root_base(),
                default_workspace_root(),
            )
    return str(default_workspace_root())


def _validate_hostname(hostname: str) -> str:
    host = hostname.strip().lower()
    if not host:
        raise ValueError('host is required')

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass

    return host


def normalize_http_origin(value: str, *, allow_explicit_port: bool) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError('host is required')

    parsed = urlsplit(raw)
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('host must start with http:// or https://')
    if not parsed.hostname:
        raise ValueError('host must include a hostname')
    if parsed.username or parsed.password:
        raise ValueError('host must not include credentials')
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('host must not include a path, query string, or fragment')
    if parsed.port is not None and not allow_explicit_port:
        raise ValueError('port must be provided in the dedicated port field')

    scheme = parsed.scheme
    host = _validate_hostname(parsed.hostname)
    if parsed.port is not None:
        return f'{scheme}://{host}:{parsed.port}'
    return f'{scheme}://{host}'


def normalize_http_host_without_port(value: str) -> str:
    return normalize_http_origin(value, allow_explicit_port=False)


def normalize_http_origin_with_optional_port(value: str) -> str:
    return normalize_http_origin(value, allow_explicit_port=True)


def normalize_smtp_host(value: str) -> str:
    host = value.strip()
    if not host:
        raise ValueError('smtp_host is required')
    if '://' in host or '/' in host or '?' in host or '#' in host or '@' in host:
        raise ValueError('smtp_host must be a plain hostname or IP address')
    return _validate_hostname(host)
