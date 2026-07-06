from __future__ import annotations

import logging
import time
from pathlib import Path
import re

import httpx
from sqlalchemy.orm import Session

from app.core import secrets_store
from app.core.security import normalize_http_host_without_port
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
    'pausedUP',   # qBittorrent < 5.0
    'stoppedUP',  # qBittorrent 5.0+ (Web API v2.11.0+): renamed from pausedUP
}
_QBT_MOVING_STATES = {'moving'}
_QBT_WAITING_STATES = {
    'queuedDL',
}
# 'pausedDL' (qBittorrent < 5.0) / 'stoppedDL' (5.0+, Web API v2.11.0+: renamed
# from pausedDL) both mean the user paused an in-progress download -- distinct
# from a transient stall the client might recover from on its own.
_QBT_PAUSED_STATES = {'pausedDL', 'stoppedDL'}

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
    if settings.password and not secrets_store.is_encrypted_secret(settings.password):
        settings.password = secrets_store.encrypt_secret(settings.password)
        db.commit()
        db.refresh(settings)
    return settings


def update_qbt_settings(db: Session, data: dict) -> QBittorrentSettings:
    settings = get_or_create_qbt_settings(db)
    for key, value in data.items():
        if key == 'password':
            if secrets_store.is_masked_secret(value):
                continue
            value = secrets_store.encrypt_secret(value or '')
        elif key == 'host' and value is not None:
            value = normalize_http_host_without_port(value)
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
        'password': secrets_store.mask_secret(s.password),
    }


# ─────────────────────────────────────────────────────────────────────────────
# qBittorrent — API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qbt_base_url(s: QBittorrentSettings) -> str:
    host = normalize_http_host_without_port(s.host or 'http://localhost').rstrip('/')
    return f"{host}:{s.port}"


def _qbt_session(s: QBittorrentSettings) -> httpx.Client:
    base = _qbt_base_url(s)
    # qBittorrent 4.1+ enforces CSRF protection: requests without a matching
    # Referer/Origin header are rejected with 403 Forbidden.
    client = httpx.Client(
        base_url=base,
        timeout=_DEFAULT_TIMEOUT,
        headers={'Referer': base, 'Origin': base},
    )
    qbt_password = secrets_store.decrypt_secret(s.password)
    resp = client.post('/api/v2/auth/login', data={'username': s.username, 'password': qbt_password})
    resp.raise_for_status()
    # qBittorrent commonly returns HTTP 200 with body "Ok."; some versions and
    # reverse-proxy setups return 204 No Content on successful login.
    login_body = resp.text.strip()
    if resp.status_code == 204:
        return client
    if login_body != 'Ok.':
        raise RuntimeError(f'qBittorrent login failed: {login_body!r}')
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
        resp = client.post('/api/v2/torrents/createTags', data={'tags': _QBT_TAG})
        if resp.status_code != 200:
            logger.warning('createTags returned HTTP %d (non-fatal)', resp.status_code)
    except Exception as exc:
        logger.warning('createTags call failed (non-fatal): %s', exc)


def tag_qbt_torrent(s: QBittorrentSettings, torrent_hash: str, max_attempts: int = 5) -> bool:
    """Tag a torrent with 'optimizarr' so it can be identified in the qBittorrent UI.

    Calls createTags first to ensure the tag exists on older qBittorrent
    versions, then retries addTags up to max_attempts times with a short
    back-off to handle the race where the torrent hasn't been indexed by qBit
    yet.  Returns True if the tag was confirmed applied, False otherwise.
    Use max_attempts=1 for a quick fire-and-check with no sleeping.
    """
    try:
        client = _qbt_session(s)
        _ensure_qbt_tag(client)
        hash_lower = torrent_hash.lower()
        for attempt in range(max_attempts):
            resp = client.post('/api/v2/torrents/addTags', data={'hashes': hash_lower, 'tags': _QBT_TAG})
            if resp.status_code != 200:
                logger.warning(
                    'addTags returned HTTP %d for torrent %s (attempt %d/%d)',
                    resp.status_code, torrent_hash, attempt + 1, max_attempts,
                )
            # Verify the tag was actually applied; the torrent may not be
            # indexed in qBit yet if this is called immediately after the grab
            # (e.g. magnet links require metadata resolution before qBit indexes
            # the torrent).  5 retries × 3 s gives ~12 s total, enough for
            # most slow-indexing scenarios.
            info = _qbt_torrent_info(client, hash_lower)
            if info is None:
                logger.debug(
                    'tag_qbt_torrent: torrent %s not found in qBit on attempt %d/%d — '
                    'torrent may not be indexed yet',
                    torrent_hash, attempt + 1, max_attempts,
                )
            elif _QBT_TAG in (info.get('tags') or ''):
                logger.info('Confirmed optimizarr tag on torrent %s (attempt %d)', torrent_hash, attempt + 1)
                return True
            else:
                logger.debug(
                    'tag_qbt_torrent: addTags sent but tag not confirmed on torrent %s '
                    '(attempt %d/%d); current tags=%r',
                    torrent_hash, attempt + 1, max_attempts, info.get('tags'),
                )
            if attempt < max_attempts - 1:
                time.sleep(3)
        logger.warning('Could not verify optimizarr tag on torrent %s after %d attempts', torrent_hash, max_attempts)
        return False
    except Exception as exc:
        logger.warning('Failed to tag qBittorrent torrent %s: %s', torrent_hash, exc)
        return False


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


def qbt_state_flags(state: str) -> dict:
    """Classify a qBittorrent torrent 'state' string into status flags.

    Shared by get_qbt_status() and download_monitor_service's replacement-
    torrent recovery paths so the state->flag mapping only lives in one place.
    """
    is_complete = state in _QBT_COMPLETE_STATES
    return {
        'is_complete': is_complete,
        'is_moving': state in _QBT_MOVING_STATES and not is_complete,
        'is_waiting': state in _QBT_WAITING_STATES and not is_complete,
        'is_paused': state in _QBT_PAUSED_STATES,
        # 'error'/'missingFiles' are terminal -- qBit will not recover these on
        # its own, unlike a transient 'stalledDL', so they should be retried
        # right away rather than waiting out the generic download timeout. A
        # user-initiated pause ('pausedDL'/'stoppedDL') is reported via
        # is_paused above, not is_stalled.
        'is_stalled': state in ('stalledDL', 'missingFiles', 'error'),
        'is_failed': state in ('error', 'missingFiles'),
    }


def get_qbt_status(s: QBittorrentSettings, torrent_hash: str) -> dict:
    """Returns progress, eta, speed, completion, and save_path for a torrent."""
    try:
        client = _qbt_session(s)
        info = _qbt_torrent_info(client, torrent_hash)
        if info is None:
            return {
                'progress_percent': 0,
                'eta_seconds': None,
                'download_speed_bps': None,
                'is_complete': False,
                'is_moving': False,
                'is_waiting': False,
                'is_paused': False,
                'is_stalled': False,
                'is_failed': False,
                'qbt_state': None,
                'save_path': None,
                'not_found': True,
            }

        state = info.get('state', '')
        progress = int(info.get('progress', 0) * 100)
        eta_raw = info.get('eta')
        eta_seconds: int | None
        try:
            eta_seconds = int(float(eta_raw))
        except (TypeError, ValueError):
            eta_seconds = None
        if eta_seconds is not None and eta_seconds < 0:
            eta_seconds = None
        dl_speed = info.get('dlspeed')
        try:
            download_speed_bps = int(float(dl_speed))
        except (TypeError, ValueError):
            download_speed_bps = None
        if download_speed_bps is not None and download_speed_bps < 0:
            download_speed_bps = None
        # content_path is the actual file/folder; fall back to save_path (directory)
        save_path = info.get('content_path') or info.get('save_path')
        return {
            'progress_percent': progress,
            'eta_seconds': eta_seconds,
            'download_speed_bps': download_speed_bps,
            **qbt_state_flags(state),
            'qbt_state': state,
            'save_path': save_path,
            'not_found': False,
        }
    except Exception as exc:
        logger.warning('qBittorrent status check failed for %s: %s', torrent_hash, exc)
        return {
            'progress_percent': 0,
            'eta_seconds': None,
            'download_speed_bps': None,
            'is_complete': False,
            'is_moving': False,
            'is_waiting': False,
            'is_paused': False,
            'is_stalled': False,
            'is_failed': False,
            'qbt_state': None,
            'save_path': None,
            'not_found': False,
        }


def remove_qbt_torrent(s: QBittorrentSettings, torrent_hash: str, *, delete_files: bool = False) -> bool:
    """Remove a torrent from qBittorrent. Returns True on success."""
    try:
        client = _qbt_session(s)
        resp = client.post(
            '/api/v2/torrents/delete',
            data={
                'hashes': str(torrent_hash or '').lower(),
                'deleteFiles': 'true' if delete_files else 'false',
            },
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning('qBittorrent: failed to remove torrent %s: %s', torrent_hash, exc)
        return False


def get_qbt_default_save_path(s: QBittorrentSettings) -> str | None:
    """Return qBittorrent's configured default completed-download directory."""
    try:
        client = _qbt_session(s)
        resp = client.get('/api/v2/app/preferences')
        resp.raise_for_status()
        payload = resp.json()
        save_path = payload.get('save_path')
        if isinstance(save_path, str) and save_path.strip():
            return save_path.strip()
    except Exception as exc:
        logger.warning('Failed to read qBittorrent default save path: %s', exc)
    return None


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
    if settings.api_key and not secrets_store.is_encrypted_secret(settings.api_key):
        settings.api_key = secrets_store.encrypt_secret(settings.api_key)
        db.commit()
        db.refresh(settings)
    return settings


def update_sab_settings(db: Session, data: dict) -> SabnzbdSettings:
    settings = get_or_create_sab_settings(db)
    for key, value in data.items():
        if key == 'api_key':
            if secrets_store.is_masked_secret(value):
                continue
            value = secrets_store.encrypt_secret(value or '')
        elif key == 'host' and value is not None:
            value = normalize_http_host_without_port(value)
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


def sab_settings_to_payload(s: SabnzbdSettings) -> dict:
    return {
        'enabled': s.enabled,
        'host': s.host,
        'port': s.port,
        'api_key': secrets_store.mask_secret(s.api_key),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SABnzbd — API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sab_base_url(s: SabnzbdSettings) -> str:
    host = normalize_http_host_without_port(s.host or 'http://localhost').rstrip('/')
    return f"{host}:{s.port}"


def _sab_api(s: SabnzbdSettings, **params) -> dict:
    url = f"{_sab_base_url(s)}/api"
    merged = {'output': 'json', 'apikey': secrets_store.decrypt_secret(s.api_key), **params}
    resp = httpx.get(url, params=merged, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _parse_sab_eta_seconds(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == '-':
        return None
    if ':' in text:
        parts = text.split(':')
        try:
            nums = [int(float(part.strip())) for part in parts]
        except ValueError:
            return None
        if len(nums) == 3:
            return max(0, (nums[0] * 3600) + (nums[1] * 60) + nums[2])
        if len(nums) == 2:
            return max(0, (nums[0] * 60) + nums[1])
        return None
    try:
        parsed = int(float(text))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _sab_call_succeeded(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get('error'):
        return False
    status = payload.get('status')
    if isinstance(status, bool):
        return status
    if status is None:
        # Some SAB endpoints return data without a "status" key.
        return True
    status_text = str(status).strip().lower()
    return status_text in {'true', 'ok', 'success', '1'}


def _sab_is_moving_status(status: object) -> bool:
    status_text = str(status or '').strip().lower()
    return status_text in {'moving', 'move'}


def _sab_is_unpacking_status(status: object) -> bool:
    status_text = str(status or '').strip().lower()
    return status_text in {
        'extracting',
        'unpacking',
    }


def _sab_is_repairing_status(status: object) -> bool:
    status_text = str(status or '').strip().lower()
    return status_text in {
        'repairing',
        'verifying',
    }


def _sab_is_waiting_status(status: object) -> bool:
    status_text = str(status or '').strip().lower()
    return status_text in {
        'queued',
        'paused',
        'idle',
    }


def _sab_status_from_snapshot(nzo_id: str, queue_data: dict, history_data: dict | None) -> dict:
    """Compute an NZO's status from an already-fetched queue/history snapshot.

    Pure function, no API calls -- shared by get_sab_status() (fetches its own
    snapshot for a single NZO) and get_sab_status_from_snapshot() (reuses one
    snapshot across many NZOs in a single monitor tick). history_data may be
    None to check only the queue, matching get_sab_status()'s lazy fetch of
    history only when the NZO isn't found in the queue.
    """
    target_nzo_id = str(nzo_id or '').strip()
    queue_info = queue_data.get('queue', {})
    # SABnzbd's top-level queue.paused flag reflects an engine-wide pause
    # (the user paused the whole queue). This is a separate signal from
    # each slot's own `status` text, which is not guaranteed to be
    # rewritten to "Paused" for a slot that was actively transferring
    # when the pause was applied.
    queue_paused = bool(queue_info.get('paused'))
    slots = queue_info.get('slots', [])
    for slot_index, slot in enumerate(slots):
        if str(slot.get('nzo_id') or '').strip() == target_nzo_id:
            pct = slot.get('percentage', 0)
            try:
                pct = int(float(pct))
            except (TypeError, ValueError):
                pct = 0
            status = slot.get('status', '')
            status_text = str(status or '').strip().lower()
            is_stalled = status_text in {'stalled', 'failed'}
            is_moving = _sab_is_moving_status(status)
            is_repairing = _sab_is_repairing_status(status)
            is_unpacking = _sab_is_unpacking_status(status)

            # Compute the slot's own reported transfer speed before
            # deciding is_waiting. SAB's queue array position alone is
            # not a fully reliable signal for "actively receiving bytes
            # right now" -- a brief article/par2 pre-check can leave a
            # not-yet-active slot with partial percentage even though
            # it's still waiting its turn, so the head slot must also
            # show real throughput, not just occupy position 0.
            speed_bps: int | None = None
            speed_sources: list[tuple[object, str]] = []
            if slot.get('mbps') is not None:
                speed_sources.append((slot.get('mbps'), 'mbps'))
            if slot.get('kbpersec') is not None:
                speed_sources.append((slot.get('kbpersec'), 'kbps'))
            if slot_index == 0 and queue_info.get('mbps') is not None:
                speed_sources.append((queue_info.get('mbps'), 'mbps'))
            if slot_index == 0 and queue_info.get('kbpersec') is not None:
                speed_sources.append((queue_info.get('kbpersec'), 'kbps'))

            for raw_value, unit_hint in speed_sources:
                speed_text = str(raw_value).strip().lower().replace(',', '.')
                try:
                    if speed_text.endswith('mb/s'):
                        speed_bps = int(float(speed_text.replace('mb/s', '').strip()) * 1024 * 1024)
                    elif speed_text.endswith('kb/s'):
                        speed_bps = int(float(speed_text.replace('kb/s', '').strip()) * 1024)
                    elif unit_hint == 'mbps':
                        speed_bps = int(float(speed_text) * 1024 * 1024)
                    else:
                        speed_bps = int(float(speed_text) * 1024)
                except ValueError:
                    speed_bps = None
                if speed_bps is not None:
                    break

            # A queue-wide pause freezes every slot except ones actively
            # post-processing (moving) or already errored (stalled/
            # failed) -- SAB keeps those running even while the download
            # queue itself is paused.
            is_queue_wide_paused = queue_paused and not is_moving and not is_repairing and not is_unpacking and not is_stalled
            has_active_speed = bool(speed_bps)
            # A slot already reporting its own "Stalled"/"Failed" status
            # text is a stronger, more specific signal than the lack of
            # throughput below -- treating it as merely "waiting its turn"
            # would park the stall/retry timeout clock forever, so a
            # stalled/failed slot must never be reclassified as waiting.
            is_waiting = (
                is_queue_wide_paused
                or _sab_is_waiting_status(status)
                or (slot_index > 0 and not is_repairing and not is_unpacking)
                or (slot_index == 0 and not is_moving and not is_repairing and not is_unpacking and not is_stalled and not has_active_speed)
            )
            # Normalize the reported status so downstream consumers see
            # "Paused" even when SAB left this slot's own status text
            # unchanged (e.g. still "Downloading") while the queue is
            # globally paused.
            effective_status = 'Paused' if is_queue_wide_paused else status
            eta_seconds = None if is_waiting else _parse_sab_eta_seconds(slot.get('timeleft'))
            if eta_seconds is None and not is_waiting and slot_index == 0:
                eta_seconds = _parse_sab_eta_seconds(queue_info.get('timeleft'))
            if is_waiting:
                speed_bps = 0
            return {
                'progress_percent': pct,
                'eta_seconds': eta_seconds,
                'download_speed_bps': speed_bps,
                'client_queue_position': slot_index,
                'is_complete': False,
                'is_moving': is_moving,
                'is_repairing': is_repairing,
                'is_unpacking': is_unpacking,
                'is_waiting': is_waiting,
                'is_stalled': is_stalled,
                # Unlike a transient stall, SAB reporting "Failed" text
                # already in the live queue means nothing further will
                # happen without a retry -- mirrors the history branch and
                # qBittorrent's error/missingFiles handling below so this
                # in-queue case also gets the immediate-retry treatment
                # instead of waiting out the generic download timeout.
                'is_failed': status_text == 'failed',
                'sab_status': effective_status,
                'save_path': None,
                'not_found': False,
            }

    if history_data is not None:
        for slot in history_data.get('history', {}).get('slots', []):
            if str(slot.get('nzo_id') or '').strip() == target_nzo_id:
                status = slot.get('status', '')
                is_complete = status == 'Completed'
                is_moving = _sab_is_moving_status(status) and not is_complete
                is_repairing = _sab_is_repairing_status(status) and not is_complete
                is_unpacking = _sab_is_unpacking_status(status) and not is_complete
                is_waiting = _sab_is_waiting_status(status) and not is_complete
                save_path = slot.get('storage') if is_complete else None
                return {
                    'progress_percent': 100 if is_complete else 0,
                    'eta_seconds': 0 if is_complete else None,
                    'download_speed_bps': 0 if is_complete else None,
                    'client_queue_position': None,
                    'is_complete': is_complete,
                    'is_moving': is_moving,
                    'is_repairing': is_repairing,
                    'is_unpacking': is_unpacking,
                    'is_waiting': is_waiting,
                    'is_stalled': status == 'Failed',
                    # A history entry means SAB has already given up on this
                    # NZO -- unlike an in-queue "Stalled" slot, nothing further
                    # will happen without a retry, so this is a terminal
                    # failure rather than a transient stall.
                    'is_failed': status == 'Failed',
                    'sab_status': status,
                    'save_path': save_path,
                    'not_found': False,
                }

    return {
        'progress_percent': 0,
        'eta_seconds': None,
        'download_speed_bps': None,
        'client_queue_position': None,
        'is_complete': False,
        'is_moving': False,
        'is_repairing': False,
        'is_unpacking': False,
        'is_waiting': False,
        'is_stalled': False,
        'is_failed': False,
        'sab_status': None,
        'save_path': None,
        'not_found': True,
    }


def get_sab_status(s: SabnzbdSettings, nzo_id: str) -> dict:
    """Returns progress, eta, speed, completion, and save_path for an NZO."""
    try:
        # Check active queue first
        queue_data = _sab_api(s, mode='queue')
        result = _sab_status_from_snapshot(nzo_id, queue_data, None)
        if not result.get('not_found'):
            return result

        # Not in the queue -- check history for a completed/failed entry.
        history_data = _sab_api(s, mode='history', limit=100)
        return _sab_status_from_snapshot(nzo_id, queue_data, history_data)
    except Exception as exc:
        logger.warning('SABnzbd status check failed for %s: %s', nzo_id, exc)
        return {
            'progress_percent': 0,
            'eta_seconds': None,
            'download_speed_bps': None,
            'client_queue_position': None,
            'is_complete': False,
            'is_moving': False,
            'is_waiting': False,
            'is_stalled': False,
            'is_failed': False,
            'sab_status': None,
            'save_path': None,
            'not_found': False,
            'status_unavailable': True,
        }


def get_sab_queue_and_history_snapshot(s: SabnzbdSettings) -> tuple[dict, dict] | tuple[None, None]:
    """Fetch SAB's queue and history once, for reuse across many status checks.

    Checking N tracked SAB jobs by calling get_sab_status() N times means N
    separate full-queue (and sometimes full-history) fetches per monitor tick
    -- with a large backlog that's 100+ redundant HTTP requests per second
    against SABnzbd's API. Callers that need to check many NZOs in one pass
    should fetch this once and pass it to get_sab_status_from_snapshot() for
    each job instead.

    Returns (None, None) if the fetch fails, so callers can fall back to the
    per-job get_sab_status() path (which has its own error handling) instead
    of silently treating a fetch failure as "not found in SAB".
    """
    try:
        queue_data = _sab_api(s, mode='queue')
        history_data = _sab_api(s, mode='history', limit=100)
        return queue_data, history_data
    except Exception as exc:
        logger.warning('SABnzbd queue/history snapshot fetch failed: %s', exc)
        return None, None


def get_sab_status_from_snapshot(nzo_id: str, queue_data: dict, history_data: dict) -> dict:
    """Same result as get_sab_status(), computed from an already-fetched snapshot.

    Pair with get_sab_queue_and_history_snapshot() to check many NZOs against
    a single fetch instead of one fetch per NZO.
    """
    return _sab_status_from_snapshot(nzo_id, queue_data, history_data)


def get_sab_queue_items(s: SabnzbdSettings) -> list[dict]:
    """Return active SAB queue entries with normalized ids/names/progress."""
    try:
        queue_data = _sab_api(s, mode='queue')
    except Exception as exc:
        logger.warning('SABnzbd queue fetch failed: %s', exc)
        return []

    slots = queue_data.get('queue', {}).get('slots', [])
    items: list[dict] = []
    for index, slot in enumerate(slots):
        nzo_id = str(slot.get('nzo_id') or '').strip()
        name = str(slot.get('filename') or slot.get('name') or slot.get('nzb_name') or '').strip()
        if not nzo_id or not name:
            continue
        try:
            percentage = float(slot.get('percentage') or 0)
        except (TypeError, ValueError):
            percentage = 0.0
        items.append({
            'nzo_id': nzo_id,
            'name': name,
            'percentage': max(0.0, min(100.0, percentage)),
            'status': str(slot.get('status') or '').strip(),
            'index': index,
        })
    return items


def _normalize_release_key(value: str) -> str:
    text = str(value or '').lower().strip()
    if not text:
        return ''
    text = re.sub(r'[\.\-_]+', ' ', text)
    return re.sub(r'[^a-z0-9]+', '', text)


def find_sab_nzo_for_release(s: SabnzbdSettings, release_name: str) -> str:
    """Best-effort lookup of SABnzbd NZO id by release title."""
    key = _normalize_release_key(release_name)
    if not key:
        return ''
    try:
        queue_data = _sab_api(s, mode='queue')
        queue_slots = queue_data.get('queue', {}).get('slots', [])
        for slot in queue_slots:
            name = str(slot.get('filename') or slot.get('name') or '')
            nzo_id = str(slot.get('nzo_id') or '').strip()
            if not nzo_id:
                continue
            slot_key = _normalize_release_key(name)
            if slot_key and (key in slot_key or slot_key in key):
                return nzo_id

        history_data = _sab_api(s, mode='history', limit=200)
        history_slots = history_data.get('history', {}).get('slots', [])
        for slot in history_slots:
            name = str(slot.get('name') or slot.get('filename') or '')
            nzo_id = str(slot.get('nzo_id') or '').strip()
            if not nzo_id:
                continue
            slot_key = _normalize_release_key(name)
            if slot_key and (key in slot_key or slot_key in key):
                return nzo_id
    except Exception as exc:
        logger.warning('SABnzbd release lookup failed for %r: %s', release_name, exc)
    return ''


def get_sab_completed_history_items(s: SabnzbdSettings, limit: int = 500) -> list[dict]:
    """Return completed SAB history entries with normalized fields."""
    try:
        history_data = _sab_api(s, mode='history', limit=max(1, int(limit)))
    except Exception as exc:
        logger.warning('SABnzbd history fetch failed: %s', exc)
        return []

    slots = history_data.get('history', {}).get('slots', [])
    items: list[dict] = []
    for slot in slots:
        if str(slot.get('status') or '').lower() != 'completed':
            continue
        nzo_id = str(slot.get('nzo_id') or '').strip()
        name = str(slot.get('name') or slot.get('filename') or slot.get('nzb_name') or '').strip()
        save_path = str(slot.get('storage') or '').strip()
        if not nzo_id or not name or not save_path:
            continue
        items.append(
            {
                'nzo_id': nzo_id,
                'name': name,
                'save_path': save_path,
            }
        )
    return items


def set_sab_category(s: SabnzbdSettings, nzo_id: str, category: str = 'optimizarr') -> bool:
    """Assign category on a SABnzbd job. Returns True if a call succeeded."""
    nzo = str(nzo_id or '').strip()
    cat = str(category or '').strip()
    if not nzo or not cat:
        return False

    attempts = (
        {'mode': 'change_cat', 'value': nzo, 'value2': cat},
        {'mode': 'queue', 'name': 'change_cat', 'value': nzo, 'value2': cat},
        {'mode': 'change_opts', 'value': nzo, 'name': 'cat', 'value2': cat},
        {'mode': 'change_opts', 'value': nzo, 'name': 'category', 'value2': cat},
        {'mode': 'queue', 'name': 'change_opts', 'value': nzo, 'cat': cat},
    )
    for params in attempts:
        try:
            payload = _sab_api(s, **params)
            if _sab_call_succeeded(payload):
                logger.info('SABnzbd: set category=%r for NZO %s', cat, nzo)
                return True
        except Exception:
            continue
    logger.warning('SABnzbd: failed setting category=%r for NZO %s', cat, nzo)
    return False


# SABnzbd priority values (see sabnzbd/constants.py): assigning a category
# resets an NZO's priority to that category's configured default unless it is
# pinned back explicitly. If the 'optimizarr' category has a non-Normal
# default (e.g. Force/High, a common setup for automation categories), every
# freshly-categorized job jumps the queue and bumps whatever was already
# downloading -- which looks like SAB abandoning a partial download to start
# another one. Pinning Normal priority right after categorizing prevents that.
_SAB_NORMAL_PRIORITY = 0


def set_sab_priority(s: SabnzbdSettings, nzo_id: str, priority: int = _SAB_NORMAL_PRIORITY) -> bool:
    """Pin an NZO's queue priority so category assignment can't reorder the queue."""
    nzo = str(nzo_id or '').strip()
    if not nzo:
        return False

    attempts = (
        {'mode': 'queue', 'name': 'priority', 'value': nzo, 'value2': priority},
        {'mode': 'change_priority', 'value': nzo, 'value2': priority},
    )
    for params in attempts:
        try:
            payload = _sab_api(s, **params)
            if _sab_call_succeeded(payload):
                logger.info('SABnzbd: set priority=%r for NZO %s', priority, nzo)
                return True
        except Exception:
            continue
    logger.warning('SABnzbd: failed setting priority=%r for NZO %s', priority, nzo)
    return False


def set_sab_queue_position(s: SabnzbdSettings, nzo_id: str, position: int) -> bool:
    """Move an NZO to a queue index. Returns True if SAB accepted the request."""
    nzo = str(nzo_id or '').strip()
    if not nzo:
        return False
    try:
        target_position = max(0, int(position))
    except (TypeError, ValueError):
        return False

    try:
        payload = _sab_api(s, mode='switch', value=nzo, value2=target_position)
        if _sab_call_succeeded(payload) or isinstance(payload.get('result') if isinstance(payload, dict) else None, dict):
            logger.info('SABnzbd: moved NZO %s to queue position %s', nzo, target_position)
            return True
    except Exception as exc:
        logger.warning('SABnzbd: failed moving NZO %s to queue position %s: %s', nzo, target_position, exc)
    return False


def set_sab_category_and_priority(
    s: SabnzbdSettings,
    nzo_id: str,
    *,
    category: str = 'optimizarr',
    priority: int = _SAB_NORMAL_PRIORITY,
) -> bool:
    """Assign category/priority without letting a new SAB job briefly jump the queue."""
    nzo = str(nzo_id or '').strip()
    if not nzo:
        return False

    original_position: int | None = None
    try:
        for item in get_sab_queue_items(s):
            if str(item.get('nzo_id') or '').strip() == nzo:
                original_position = int(item.get('index'))
                break
    except Exception:
        original_position = None

    paused_for_update = set_sab_priority(s, nzo, -2)
    category_ok = set_sab_category(s, nzo, category=category)
    priority_ok = set_sab_priority(s, nzo, priority)

    if original_position is not None and original_position > 0:
        set_sab_queue_position(s, nzo, original_position)

    if not paused_for_update:
        logger.warning('SABnzbd: could not pause NZO %s before category/priority update', nzo)
    return category_ok and priority_ok


def delete_sab_history(s: SabnzbdSettings, nzo_id: str) -> None:
    """Remove a completed download from SABnzbd history (does not delete files)."""
    try:
        _sab_api(s, mode='history', name='delete', del_files=0, value=nzo_id)
        logger.info('SABnzbd: deleted history entry %s', nzo_id)
    except Exception as exc:
        logger.warning('SABnzbd: failed to delete history entry %s: %s', nzo_id, exc)


def remove_sab_job(s: SabnzbdSettings, nzo_id: str, *, delete_files: bool = False) -> bool:
    """Remove an item from the SABnzbd queue and history. Returns True on success."""
    try:
        _sab_api(s, mode='queue', name='delete', value=nzo_id, del_files=1 if delete_files else 0)
        # If the item already moved to history, this call safely no-ops.
        _sab_api(s, mode='history', name='delete', value=nzo_id, del_files=1 if delete_files else 0)
        return True
    except Exception as exc:
        logger.warning('SABnzbd: failed to remove NZO %s: %s', nzo_id, exc)
        return False


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
    return {
        'progress_percent': 0,
        'eta_seconds': None,
        'download_speed_bps': None,
        'client_queue_position': None,
        'is_complete': False,
        'is_moving': False,
        'is_waiting': False,
        'is_stalled': False,
        'qbt_state': None,
        'save_path': None,
        'not_found': False,
        'status_unavailable': True,
    }


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
