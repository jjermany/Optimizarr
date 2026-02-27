from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.models.prowlarr_settings import ProwlarrSettings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15


def get_or_create_prowlarr_settings(db: Session) -> ProwlarrSettings:
    settings = db.query(ProwlarrSettings).first()
    if settings is None:
        settings = ProwlarrSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, data: dict) -> ProwlarrSettings:
    settings = get_or_create_prowlarr_settings(db)
    for key, value in data.items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def settings_to_payload(settings: ProwlarrSettings) -> dict:
    return {
        'enabled': settings.enabled,
        'host': settings.host,
        'api_key': settings.api_key,
    }


def test_connection(settings: ProwlarrSettings) -> dict:
    """Test connectivity to Prowlarr by fetching the indexer list."""
    try:
        url = f"{settings.host.rstrip('/')}/api/v1/indexer"
        resp = httpx.get(
            url,
            headers={'X-Api-Key': settings.api_key},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        indexers = resp.json()
        return {'success': True, 'indexer_count': len(indexers)}
    except httpx.HTTPStatusError as exc:
        return {'success': False, 'error': f'HTTP {exc.response.status_code}: {exc.response.text[:200]}'}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


def search(settings: ProwlarrSettings, query: str, categories: list[int] | None = None) -> list[dict] | None:
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
        url = f"{settings.host.rstrip('/')}/api/v1/search"
        params: dict = {'query': query, 'limit': 50}
        if categories:
            # Prowlarr accepts repeated 'categories' params
            params['categories'] = categories

        resp = httpx.get(
            url,
            params=params,
            headers={'X-Api-Key': settings.api_key},
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


def grab(settings: ProwlarrSettings, guid: str, indexer_id: int) -> dict | None:
    """
    Send a release to the configured download client via Prowlarr's grab endpoint.
    Returns the response dict (may contain downloadId/hash) or None on failure.
    """
    try:
        url = f"{settings.host.rstrip('/')}/api/v1/search"
        payload = {'guid': guid, 'indexerId': indexer_id}
        resp = httpx.post(
            url,
            json=payload,
            headers={'X-Api-Key': settings.api_key},
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
