from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.core import secrets_store
from app.core.security import normalize_http_origin_with_optional_port
from app.models.prowlarr_settings import ProwlarrSettings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15
_SEARCH_RESULT_LIMIT = 100


def get_or_create_prowlarr_settings(db: Session) -> ProwlarrSettings:
    settings = db.query(ProwlarrSettings).first()
    if settings is None:
        settings = ProwlarrSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    if settings.api_key and not secrets_store.is_encrypted_secret(settings.api_key):
        settings.api_key = secrets_store.encrypt_secret(settings.api_key)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, data: dict) -> ProwlarrSettings:
    settings = get_or_create_prowlarr_settings(db)
    for key, value in data.items():
        if key == 'api_key':
            if secrets_store.is_masked_secret(value):
                continue
            value = secrets_store.encrypt_secret(value or '')
        elif key == 'host' and value is not None:
            value = normalize_http_origin_with_optional_port(value)
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def settings_to_payload(settings: ProwlarrSettings) -> dict:
    return {
        'enabled': settings.enabled,
        'host': settings.host,
        'api_key': secrets_store.mask_secret(settings.api_key),
    }


def test_connection(settings: ProwlarrSettings) -> dict:
    """Test connectivity to Prowlarr by fetching the indexer list."""
    try:
        api_key = secrets_store.decrypt_secret(settings.api_key)
        url = f"{settings.host.rstrip('/')}/api/v1/indexer"
        resp = httpx.get(
            url,
            headers={'X-Api-Key': api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        indexers = resp.json()
        return {'success': True, 'indexer_count': len(indexers)}
    except httpx.HTTPStatusError as exc:
        return {'success': False, 'error': f'HTTP {exc.response.status_code}: {exc.response.text[:200]}'}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


def search(
    settings: ProwlarrSettings,
    query: str,
    categories: list[int] | None = None,
    search_type: str | None = None,
) -> list[dict] | None:
    """
    Search Prowlarr indexers for a query string.

    Categories: 2000 = Movies, 5000 = TV
    Returns a list of release dicts with keys: guid, indexerId, title, size, seeders, downloadUrl, etc.
    Returns None on a connection/HTTP error so callers can distinguish a
    transient failure from a successful search that returned zero results.
    """
    if categories is None:
        categories = [2000, 5000]

    try:
        api_key = secrets_store.decrypt_secret(settings.api_key)
        url = f"{settings.host.rstrip('/')}/api/v1/search"
        params: dict = {'query': query, 'limit': _SEARCH_RESULT_LIMIT}
        if search_type:
            params['type'] = search_type
        if categories:
            # Prowlarr accepts repeated 'categories' params
            params['categories'] = categories

        resp = httpx.get(
            url,
            params=params,
            headers={'X-Api-Key': api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning('Prowlarr search failed HTTP %s: %s', exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:
        logger.warning('Prowlarr search error: %s', exc)
        return None


def get_download_clients(settings: ProwlarrSettings) -> list[dict]:
    """Return configured Prowlarr download clients."""
    try:
        api_key = secrets_store.decrypt_secret(settings.api_key)
        url = f"{settings.host.rstrip('/')}/api/v1/downloadclient"
        resp = httpx.get(
            url,
            headers={'X-Api-Key': api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else []
    except Exception as exc:
        logger.warning('Prowlarr download clients query failed: %s', exc)
        return []


def get_indexers(settings: ProwlarrSettings) -> list[dict]:
    """Return configured Prowlarr indexers (includes priority metadata)."""
    try:
        api_key = secrets_store.decrypt_secret(settings.api_key)
        url = f"{settings.host.rstrip('/')}/api/v1/indexer"
        resp = httpx.get(
            url,
            headers={'X-Api-Key': api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else []
    except Exception as exc:
        logger.warning('Prowlarr indexer query failed: %s', exc)
        return []


def grab(
    settings: ProwlarrSettings,
    guid: str,
    indexer_id: int,
    download_client_id: int | None = None,
) -> dict | None:
    """
    Send a release to the configured download client via Prowlarr's grab endpoint.
    Returns the response dict (may contain downloadId/hash) or None on failure.
    """
    try:
        api_key = secrets_store.decrypt_secret(settings.api_key)
        url = f"{settings.host.rstrip('/')}/api/v1/search"
        payload: dict[str, int | str] = {'guid': guid, 'indexerId': indexer_id}
        if isinstance(download_client_id, int):
            payload['downloadClientId'] = download_client_id
        resp = httpx.post(
            url,
            json=payload,
            headers={'X-Api-Key': api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning('Prowlarr grab failed HTTP %s: %s', exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:
        logger.warning('Prowlarr grab error: %s', exc)
        return None
