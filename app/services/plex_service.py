from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core import secrets_store
from app.core.security import normalize_http_host_without_port
from app.models.library import LibraryProfile
from app.models.plex_settings import PlexSettings

logger = logging.getLogger(__name__)


def get_or_create_plex_settings(db: Session) -> PlexSettings:
    settings = db.query(PlexSettings).first()
    if not settings:
        settings = PlexSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    if settings.token and not secrets_store.is_encrypted_secret(settings.token):
        settings.token = secrets_store.encrypt_secret(settings.token)
        db.commit()
        db.refresh(settings)
    return settings


def settings_to_payload(settings: PlexSettings) -> dict:
    return {
        'enabled': settings.enabled,
        'host': settings.host,
        'port': settings.port,
        'token': secrets_store.mask_secret(settings.token),
    }


def update_settings(db: Session, payload: dict) -> PlexSettings:
    settings = get_or_create_plex_settings(db)

    for key in ['enabled', 'host', 'port']:
        if key in payload:
            value = payload[key]
            if key == 'host' and value is not None:
                value = normalize_http_host_without_port(value)
            setattr(settings, key, value)
    if 'token' in payload:
        raw = payload['token'] or ''
        if not secrets_store.is_masked_secret(raw):
            settings.token = secrets_store.encrypt_secret(raw)

    db.commit()
    db.refresh(settings)
    return settings


def _build_base_url(settings: PlexSettings) -> str:
    host = normalize_http_host_without_port(settings.host or 'http://localhost').rstrip('/')
    port = int(settings.port or 32400)
    return f'{host}:{port}'


def _plex_headers(token: str) -> dict:
    return {
        'X-Plex-Token': token,
        'Accept': 'application/json',
    }


def _trigger_section_scan(section_id: str, settings: PlexSettings) -> None:
    base_url = _build_base_url(settings)
    url = f'{base_url}/library/sections/{section_id}/refresh'
    token = secrets_store.decrypt_secret(settings.token)
    with httpx.Client(timeout=10) as client:
        response = client.get(url, headers=_plex_headers(token))
        response.raise_for_status()
    logger.debug('Triggered Plex scan for section %s', section_id)


def fetch_plex_libraries() -> list[dict]:
    """Fetch available library sections from the configured Plex server."""
    db = SessionLocal()
    try:
        settings = get_or_create_plex_settings(db)
        token = secrets_store.decrypt_secret(settings.token)
        if not token:
            return []

        base_url = _build_base_url(settings)
        url = f'{base_url}/library/sections'
        with httpx.Client(timeout=10) as client:
            response = client.get(url, headers=_plex_headers(token))
            response.raise_for_status()

        data = response.json()
        directories = data.get('MediaContainer', {}).get('Directory', [])
        return [
            {'id': str(d.get('key', '')), 'name': d.get('title', ''), 'type': d.get('type', '')}
            for d in directories
            if d.get('key')
        ]
    except Exception:
        logger.exception('Failed to fetch Plex library sections')
        return []
    finally:
        db.close()


def trigger_scan_after_job(library_id: int | None) -> None:
    """Trigger a Plex scan for the section mapped to the given Optimizarr library."""
    db = SessionLocal()
    try:
        settings = get_or_create_plex_settings(db)
        if not settings.enabled:
            return
        if not secrets_store.decrypt_secret(settings.token):
            logger.warning('Plex integration enabled but no token configured; skipping scan')
            return

        if library_id is None:
            logger.debug('Job has no associated library; skipping Plex scan')
            return

        profile = db.query(LibraryProfile).filter(LibraryProfile.library_id == library_id).first()
        if not profile or not profile.plex_library_id:
            logger.debug('No Plex section mapped for library %s; skipping scan', library_id)
            return

        try:
            _trigger_section_scan(profile.plex_library_id, settings)
        except Exception:
            logger.exception(
                'Failed to trigger Plex scan for section %s (library %s)',
                profile.plex_library_id,
                library_id,
            )
    finally:
        db.close()


def test_plex_connection() -> dict:
    """Test connectivity to the configured Plex server. Returns success/error dict."""
    db = SessionLocal()
    try:
        settings = get_or_create_plex_settings(db)
        token = secrets_store.decrypt_secret(settings.token)
        if not token:
            return {'success': False, 'error': 'No token configured'}

        base_url = _build_base_url(settings)
        url = f'{base_url}/library/sections'
        with httpx.Client(timeout=10) as client:
            response = client.get(url, headers=_plex_headers(token))
            response.raise_for_status()

        return {'success': True}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}
    finally:
        db.close()
