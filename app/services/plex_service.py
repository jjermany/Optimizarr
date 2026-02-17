from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.plex_settings import PlexSettings

logger = logging.getLogger(__name__)


def get_or_create_plex_settings(db: Session) -> PlexSettings:
    settings = db.query(PlexSettings).first()
    if not settings:
        settings = PlexSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _library_ids_from_csv(csv_value: str) -> list[str]:
    return [lid.strip() for lid in csv_value.split(',') if lid.strip()]


def _library_ids_to_csv(ids: list[str]) -> str:
    return ','.join(lid.strip() for lid in ids if lid.strip())


def settings_to_payload(settings: PlexSettings) -> dict:
    return {
        'enabled': settings.enabled,
        'host': settings.host,
        'port': settings.port,
        'token': settings.token,
        'library_ids': _library_ids_from_csv(settings.library_ids),
    }


def update_settings(db: Session, payload: dict) -> PlexSettings:
    settings = get_or_create_plex_settings(db)

    for key in ['enabled', 'host', 'port', 'token']:
        if key in payload:
            setattr(settings, key, payload[key])

    if 'library_ids' in payload:
        settings.library_ids = _library_ids_to_csv(payload['library_ids'])

    db.commit()
    db.refresh(settings)
    return settings


def _build_base_url(settings: PlexSettings) -> str:
    host = (settings.host or 'http://localhost').rstrip('/')
    port = int(settings.port or 32400)
    return f'{host}:{port}'


def _trigger_section_scan(section_id: str, settings: PlexSettings) -> None:
    base_url = _build_base_url(settings)
    url = f'{base_url}/library/sections/{section_id}/refresh'
    params = {'X-Plex-Token': settings.token}
    with httpx.Client(timeout=10) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
    logger.debug('Triggered Plex scan for section %s', section_id)


def trigger_scan_after_job() -> None:
    """Trigger Plex library scans. Called after a job completes successfully."""
    db = SessionLocal()
    try:
        settings = get_or_create_plex_settings(db)
        if not settings.enabled:
            return
        if not settings.token:
            logger.warning('Plex integration enabled but no token configured; skipping scan')
            return

        library_ids = _library_ids_from_csv(settings.library_ids)
        if not library_ids:
            logger.warning('Plex integration enabled but no library section IDs configured; skipping scan')
            return

        for section_id in library_ids:
            try:
                _trigger_section_scan(section_id, settings)
            except Exception:
                logger.exception('Failed to trigger Plex scan for section %s', section_id)
    finally:
        db.close()


def test_plex_connection() -> dict:
    """Test connectivity to the configured Plex server. Returns success/error dict."""
    db = SessionLocal()
    try:
        settings = get_or_create_plex_settings(db)
        if not settings.token:
            return {'success': False, 'error': 'No token configured'}

        base_url = _build_base_url(settings)
        url = f'{base_url}/library/sections'
        params = {'X-Plex-Token': settings.token}

        with httpx.Client(timeout=10) as client:
            response = client.get(url, params=params)
            response.raise_for_status()

        return {'success': True}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}
    finally:
        db.close()
