from __future__ import annotations

import logging
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.models.download_client_settings import DownloadClientSettings, DownloadClientTypeEnum

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15

# qBittorrent torrent states that indicate a completed download
_QBT_COMPLETE_STATES = {
    'uploading',
    'stalledUP',
    'seeding',
    'forcedUP',
    'queuedUP',
    'checkingUP',
    'pausedUP',
}

MEDIA_SUFFIXES = {'.mkv', '.mp4'}


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_settings(db: Session) -> DownloadClientSettings:
    settings = db.query(DownloadClientSettings).first()
    if settings is None:
        settings = DownloadClientSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, data: dict) -> DownloadClientSettings:
    settings = get_or_create_settings(db)
    for key, value in data.items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def settings_to_payload(settings: DownloadClientSettings) -> dict:
    return {
        'enabled': settings.enabled,
        'client_type': settings.client_type,
        'host': settings.host,
        'port': settings.port,
        'username': settings.username,
        'password': settings.password,
        'api_key': settings.api_key,
    }


# ─────────────────────────────────────────────────────────────────────────────
# qBittorrent helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qbt_base_url(settings: DownloadClientSettings) -> str:
    host = settings.host.rstrip('/')
    return f"{host}:{settings.port}"


def _qbt_session(settings: DownloadClientSettings) -> httpx.Client:
    """Return an authenticated httpx.Client for qBittorrent."""
    base = _qbt_base_url(settings)
    client = httpx.Client(base_url=base, timeout=_DEFAULT_TIMEOUT)
    resp = client.post(
        '/api/v2/auth/login',
        data={'username': settings.username, 'password': settings.password},
    )
    resp.raise_for_status()
    return client


def _qbt_torrent_info(client: httpx.Client, torrent_hash: str) -> dict | None:
    """Fetch info for a single torrent by hash."""
    resp = client.get('/api/v2/torrents/info', params={'hashes': torrent_hash})
    resp.raise_for_status()
    items = resp.json()
    return items[0] if items else None


def _qbt_status(settings: DownloadClientSettings, torrent_hash: str) -> dict:
    """
    Returns:
        progress_percent (int), is_complete (bool), is_stalled (bool), save_path (str|None)
    """
    try:
        client = _qbt_session(settings)
        info = _qbt_torrent_info(client, torrent_hash)
        if info is None:
            return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}

        state = info.get('state', '')
        progress = int(info.get('progress', 0) * 100)
        is_complete = state in _QBT_COMPLETE_STATES
        # Stalled states while downloading
        is_stalled = state in ('stalledDL', 'missingFiles', 'error')
        # content_path is the actual file/folder path, save_path is the directory
        save_path = info.get('content_path') or info.get('save_path')
        return {
            'progress_percent': progress,
            'is_complete': is_complete,
            'is_stalled': is_stalled,
            'save_path': save_path,
        }
    except Exception as exc:
        logger.warning('qBittorrent status check failed for %s: %s', torrent_hash, exc)
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


def _qbt_test(settings: DownloadClientSettings) -> dict:
    try:
        client = _qbt_session(settings)
        resp = client.get('/api/v2/app/version')
        resp.raise_for_status()
        return {'success': True, 'version': resp.text.strip()}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# SABnzbd helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sab_base_url(settings: DownloadClientSettings) -> str:
    host = settings.host.rstrip('/')
    return f"{host}:{settings.port}"


def _sab_api(settings: DownloadClientSettings, **params) -> dict:
    url = f"{_sab_base_url(settings)}/api"
    merged = {'output': 'json', 'apikey': settings.api_key, **params}
    resp = httpx.get(url, params=merged, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _sab_status(settings: DownloadClientSettings, nzo_id: str) -> dict:
    """
    Check queue first, then history for a completed entry.
    Returns: progress_percent, is_complete, is_stalled, save_path
    """
    try:
        # Check active queue
        queue_data = _sab_api(settings, mode='queue')
        for slot in queue_data.get('queue', {}).get('slots', []):
            if slot.get('nzo_id') == nzo_id:
                pct = slot.get('percentage', 0)
                try:
                    pct = int(float(pct))
                except (TypeError, ValueError):
                    pct = 0
                status = slot.get('status', '')
                is_stalled = status in ('Stalled', 'Failed')
                return {'progress_percent': pct, 'is_complete': False, 'is_stalled': is_stalled, 'save_path': None}

        # Check history for completion
        history_data = _sab_api(settings, mode='history', limit=100)
        for slot in history_data.get('history', {}).get('slots', []):
            if slot.get('nzo_id') == nzo_id:
                status = slot.get('status', '')
                is_complete = status == 'Completed'
                save_path = slot.get('storage') if is_complete else None
                return {'progress_percent': 100 if is_complete else 0, 'is_complete': is_complete, 'is_stalled': status == 'Failed', 'save_path': save_path}

        # Not found in queue or history
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}
    except Exception as exc:
        logger.warning('SABnzbd status check failed for %s: %s', nzo_id, exc)
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


def _sab_test(settings: DownloadClientSettings) -> dict:
    try:
        data = _sab_api(settings, mode='version')
        return {'success': True, 'version': data.get('version', 'unknown')}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Unified interface
# ─────────────────────────────────────────────────────────────────────────────

def get_download_status(settings: DownloadClientSettings, download_hash: str) -> dict:
    """
    Returns dict: {progress_percent, is_complete, is_stalled, save_path}
    save_path is the file or directory path returned by the client on completion.
    """
    if settings.client_type == DownloadClientTypeEnum.qbittorrent:
        return _qbt_status(settings, download_hash)
    elif settings.client_type == DownloadClientTypeEnum.sabnzbd:
        return _sab_status(settings, download_hash)
    else:
        logger.error('Unknown download client type: %s', settings.client_type)
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


def test_connection(settings: DownloadClientSettings) -> dict:
    if settings.client_type == DownloadClientTypeEnum.qbittorrent:
        return _qbt_test(settings)
    elif settings.client_type == DownloadClientTypeEnum.sabnzbd:
        return _sab_test(settings)
    else:
        return {'success': False, 'error': f'Unknown client type: {settings.client_type}'}


def find_video_in_path(path: str) -> Path | None:
    """
    Given a path (file or directory from the download client), find the largest
    video file with a supported extension.
    """
    candidate = Path(path)

    if candidate.is_file():
        if candidate.suffix.lower() in MEDIA_SUFFIXES:
            return candidate
        return None

    if candidate.is_dir():
        matches = []
        for suffix in MEDIA_SUFFIXES:
            matches.extend(candidate.rglob(f'*{suffix}'))
        if matches:
            return max(matches, key=lambda p: p.stat().st_size)

    return None
