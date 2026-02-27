from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.models.qbittorrent_settings import QBittorrentSettings
from app.models.sabnzbd_settings import SabnzbdSettings

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

_QBT_TAG = 'optimizarr'


# ─────────────────────────────────────────────────────────────────────────────
# qBittorrent — persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_qbt_settings(db: Session) -> QBittorrentSettings:
    settings = db.query(QBittorrentSettings).filter(QBittorrentSettings.id == 1).first()
    if settings is None:
        settings = QBittorrentSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_qbt_settings(db: Session, data: dict) -> QBittorrentSettings:
    settings = get_or_create_qbt_settings(db)
    for key, value in data.items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def qbt_settings_to_payload(s: QBittorrentSettings) -> dict:
    return {
        'enabled': s.enabled,
        'host': s.host,
        'port': s.port,
        'username': s.username,
        'password': s.password,
    }


# ─────────────────────────────────────────────────────────────────────────────
# qBittorrent — API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qbt_base_url(s: QBittorrentSettings) -> str:
    return f"{s.host.rstrip('/')}:{s.port}"


def _qbt_session(s: QBittorrentSettings) -> httpx.Client:
    base = _qbt_base_url(s)
    # qBittorrent 4.1+ enforces CSRF protection: requests without a matching
    # Referer/Origin header are rejected with 403 Forbidden.
    client = httpx.Client(
        base_url=base,
        timeout=_DEFAULT_TIMEOUT,
        headers={'Referer': base, 'Origin': base},
    )
    resp = client.post('/api/v2/auth/login', data={'username': s.username, 'password': s.password})
    resp.raise_for_status()
    # qBittorrent returns HTTP 200 with body "Fails." on bad credentials.
    if resp.text.strip() != 'Ok.':
        raise RuntimeError(f'qBittorrent login failed: {resp.text.strip()!r}')
    return client


def _qbt_torrent_info(client: httpx.Client, torrent_hash: str) -> dict | None:
    # qBittorrent stores hashes in lowercase; normalise before querying.
    resp = client.get('/api/v2/torrents/info', params={'hashes': torrent_hash.lower()})
    resp.raise_for_status()
    items = resp.json()
    return items[0] if items else None


def _ensure_qbt_tag(client: httpx.Client) -> None:
    """Create the 'optimizarr' tag in qBittorrent if it does not already exist.

    Older qBittorrent versions (< 4.4) require the tag to be created via
    /api/v2/torrents/createTags before it can be applied; calling addTags on a
    non-existent tag silently does nothing on those versions.
    """
    try:
        client.post('/api/v2/torrents/createTags', data={'tags': _QBT_TAG})
    except Exception as exc:
        logger.debug('createTags call failed (non-fatal): %s', exc)


def tag_qbt_torrent(s: QBittorrentSettings, torrent_hash: str) -> None:
    """Tag a torrent with 'optimizarr' so it can be identified in the qBittorrent UI.

    Calls createTags first to ensure the tag exists on older qBittorrent
    versions, then retries addTags up to 3 times with a short back-off to
    handle the race where the torrent hasn't been indexed by qBit yet.
    """
    try:
        client = _qbt_session(s)
        _ensure_qbt_tag(client)
        hash_lower = torrent_hash.lower()
        for attempt in range(3):
            client.post('/api/v2/torrents/addTags', data={'hashes': hash_lower, 'tags': _QBT_TAG})
            # Verify the tag was actually applied; the torrent may not be
            # indexed in qBit yet if this is called immediately after the grab.
            info = _qbt_torrent_info(client, hash_lower)
            if info and _QBT_TAG in (info.get('tags') or ''):
                return  # success
            if attempt < 2:
                time.sleep(2)
        logger.warning('Could not verify optimizarr tag on torrent %s after 3 attempts', torrent_hash)
    except Exception as exc:
        logger.warning('Failed to tag qBittorrent torrent %s: %s', torrent_hash, exc)


def get_all_qbt_tagged_torrents(s: QBittorrentSettings) -> list[dict]:
    """Return all torrents tagged with 'optimizarr' from qBittorrent.

    Used on startup to reconcile in-flight download jobs against what qBit
    actually has, including finding downloads that completed while offline.
    """
    try:
        client = _qbt_session(s)
        resp = client.get('/api/v2/torrents/info', params={'tag': _QBT_TAG})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning('Failed to fetch tagged torrents from qBittorrent: %s', exc)
        return []


def get_all_qbt_torrents(s: QBittorrentSettings) -> list[dict]:
    """Return ALL torrents from qBittorrent (no tag filter).

    Used to recover a torrent hash when Prowlarr's grab response didn't
    include one.  Each dict includes at minimum: hash, name, added_on,
    state, content_path, save_path, progress.
    """
    try:
        client = _qbt_session(s)
        resp = client.get('/api/v2/torrents/info')
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning('Failed to fetch all torrents from qBittorrent: %s', exc)
        return []


def get_qbt_status(s: QBittorrentSettings, torrent_hash: str) -> dict:
    """Returns progress_percent, is_complete, is_stalled, save_path."""
    try:
        client = _qbt_session(s)
        info = _qbt_torrent_info(client, torrent_hash)
        if info is None:
            return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}

        state = info.get('state', '')
        progress = int(info.get('progress', 0) * 100)
        is_complete = state in _QBT_COMPLETE_STATES
        is_stalled = state in ('stalledDL', 'missingFiles', 'error')
        # content_path is the actual file/folder; fall back to save_path (directory)
        save_path = info.get('content_path') or info.get('save_path')
        return {'progress_percent': progress, 'is_complete': is_complete, 'is_stalled': is_stalled, 'save_path': save_path}
    except Exception as exc:
        logger.warning('qBittorrent status check failed for %s: %s', torrent_hash, exc)
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


def test_qbt_connection(s: QBittorrentSettings) -> dict:
    try:
        client = _qbt_session(s)
        resp = client.get('/api/v2/app/version')
        resp.raise_for_status()
        return {'success': True, 'version': resp.text.strip()}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# SABnzbd — persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_sab_settings(db: Session) -> SabnzbdSettings:
    settings = db.query(SabnzbdSettings).filter(SabnzbdSettings.id == 1).first()
    if settings is None:
        settings = SabnzbdSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_sab_settings(db: Session, data: dict) -> SabnzbdSettings:
    settings = get_or_create_sab_settings(db)
    for key, value in data.items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def sab_settings_to_payload(s: SabnzbdSettings) -> dict:
    return {
        'enabled': s.enabled,
        'host': s.host,
        'port': s.port,
        'api_key': s.api_key,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SABnzbd — API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sab_base_url(s: SabnzbdSettings) -> str:
    return f"{s.host.rstrip('/')}:{s.port}"


def _sab_api(s: SabnzbdSettings, **params) -> dict:
    url = f"{_sab_base_url(s)}/api"
    merged = {'output': 'json', 'apikey': s.api_key, **params}
    resp = httpx.get(url, params=merged, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_sab_status(s: SabnzbdSettings, nzo_id: str) -> dict:
    """Returns progress_percent, is_complete, is_stalled, save_path."""
    try:
        # Check active queue first
        queue_data = _sab_api(s, mode='queue')
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

        # Check history for completed entry
        history_data = _sab_api(s, mode='history', limit=100)
        for slot in history_data.get('history', {}).get('slots', []):
            if slot.get('nzo_id') == nzo_id:
                status = slot.get('status', '')
                is_complete = status == 'Completed'
                save_path = slot.get('storage') if is_complete else None
                return {
                    'progress_percent': 100 if is_complete else 0,
                    'is_complete': is_complete,
                    'is_stalled': status == 'Failed',
                    'save_path': save_path,
                }

        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}
    except Exception as exc:
        logger.warning('SABnzbd status check failed for %s: %s', nzo_id, exc)
        return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


def delete_sab_history(s: SabnzbdSettings, nzo_id: str) -> None:
    """Remove a completed download from SABnzbd history (does not delete files)."""
    try:
        _sab_api(s, mode='history', name='delete', del_files=0, value=nzo_id)
        logger.info('SABnzbd: deleted history entry %s', nzo_id)
    except Exception as exc:
        logger.warning('SABnzbd: failed to delete history entry %s: %s', nzo_id, exc)


def test_sab_connection(s: SabnzbdSettings) -> dict:
    try:
        data = _sab_api(s, mode='version')
        return {'success': True, 'version': data.get('version', 'unknown')}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Unified status check (dispatches by client_type string)
# ─────────────────────────────────────────────────────────────────────────────

def get_download_status(client_type: str, qbt: QBittorrentSettings | None, sab: SabnzbdSettings | None, download_hash: str) -> dict:
    """Dispatch status check to the appropriate client based on client_type."""
    if client_type == 'qbittorrent' and qbt is not None:
        return get_qbt_status(qbt, download_hash)
    if client_type == 'sabnzbd' and sab is not None:
        return get_sab_status(sab, download_hash)
    logger.error('get_download_status: unknown client_type=%r or missing settings', client_type)
    return {'progress_percent': 0, 'is_complete': False, 'is_stalled': False, 'save_path': None}


# ─────────────────────────────────────────────────────────────────────────────
# File discovery
# ─────────────────────────────────────────────────────────────────────────────

def find_video_in_path(path: str) -> Path | None:
    """
    Given a path (file or directory from the download client's complete folder),
    find the largest video file with a supported extension.
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
