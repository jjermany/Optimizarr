from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job
from app.models.library import DownloadQualityProfileEnum, Library, LibraryProfile
from app.models.settings import QueueSortEnum, Settings
from app.services import download_client_service, notification_service, prowlarr_service
from app.services.job_service import cleanup_replaced_optimized_outputs, create_job, media_identity_key
from app.services.optimization_service import is_hdr_video, probe_video_height, stop_active_ffmpeg
from app.services.realtime_service import broker

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
# Signalled whenever a new DownloadJob is created so the loop wakes immediately
_wake_event = threading.Event()
_thread: threading.Thread | None = None
# Set while a post-scan recovery run is in progress; blocks new Prowlarr searches
_scan_recovery_event = threading.Event()

# When set, the search pipeline is halted after an import error so the user
# can investigate before more downloads are started.
_download_queue_stopped: bool = False
_download_queue_stop_reason: str = ''

# Track which DownloadJob IDs have had their qBittorrent tag confirmed.
# In-memory only; entries are re-added after restart on the first progress check.
_tagged_job_ids: set[int] = set()
# Track which DownloadJob IDs have had their SABnzbd category confirmed.
# In-memory only; entries are retried on restart/progress checks as needed.
_categorized_sab_job_ids: set[int] = set()
_DEFAULT_DOWNLOAD_MAX_RETRIES = 5
_CLIENT_TRACKING_GRACE_SECONDS = 10
_QBT_STRIKE_CHECK_INTERVAL_SECONDS = 60
_QBT_METADATA_MAX_STRIKES = 3
_QBT_STALLED_MAX_STRIKES = 3
_QBT_SLOW_MAX_STRIKES = 3
_QBT_SLOW_MIN_SPEED_BPS = 0
_QBT_METADATA_STATES = {'metaDL', 'forcedMetaDL'}
_QBT_STALE_STATES = {'stalledDL', 'missingFiles', 'error', 'stoppedDL'}
_qbt_strike_state: dict[str, dict[str, object]] = {}
_last_qbt_strike_cleanup_monotonic = 0.0
_UNWANTED_RELEASE_VARIANT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'\bsing[\s._-]*along\b', re.IGNORECASE),
    re.compile(r"\bdirector'?s[\s._-]*cut\b", re.IGNORECASE),
    re.compile(r'\bextended\b', re.IGNORECASE),
    re.compile(r'\btheatrical\b', re.IGNORECASE),
    re.compile(r'\bimax\b', re.IGNORECASE),
    re.compile(r'\bopen[\s._-]*matte\b', re.IGNORECASE),
    re.compile(r'\bunrated\b', re.IGNORECASE),
    re.compile(r'\buncut\b', re.IGNORECASE),
)
_UNWANTED_RELEASE_VARIANT_REQUIRED_TOKENS: dict[re.Pattern[str], tuple[str, ...]] = {
    _UNWANTED_RELEASE_VARIANT_PATTERNS[0]: ('sing', 'along', 'version'),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[1]: ('director', 'cut'),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[2]: ('extended',),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[3]: ('theatrical',),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[4]: ('imax',),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[5]: ('open', 'matte'),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[6]: ('unrated',),
    _UNWANTED_RELEASE_VARIANT_PATTERNS[7]: ('uncut',),
}


def is_download_queue_stopped() -> bool:
    return _download_queue_stopped


def get_download_queue_stop_reason() -> str:
    return _download_queue_stop_reason


def resume_download_queue() -> None:
    global _download_queue_stopped, _download_queue_stop_reason
    _download_queue_stopped = False
    _download_queue_stop_reason = ''
    _wake_event.set()
    logger.info('Download queue resumed by user')


def _stop_download_queue(reason: str) -> None:
    global _download_queue_stopped, _download_queue_stop_reason
    _download_queue_stopped = True
    _download_queue_stop_reason = reason
    logger.warning('Download queue stopped: %s', reason)
    broker.publish_notification(f'Download queue stopped due to error: {reason}')


# Adaptive poll intervals
_POLL_DOWNLOADING_SECONDS = 1   # fast polling while files are transferring
_POLL_SEARCHING_SECONDS = 10    # moderate polling while waiting for Prowlarr
_POLL_IDLE_SECONDS = 30         # slow polling when nothing is active
_IDLE_RECOVERY_INTERVAL_SECONDS = 30
_last_idle_recovery_monotonic = 0.0

# Startup grace period: holds off Prowlarr grabs for this many seconds after a
# restart so the user has time to review / remove queued jobs before they fire.
_STARTUP_GRACE_SECONDS = 60
_startup_grace_until: datetime | None = None

# Optional callback invoked when a download job reaches a terminal state
# (complete or permanently failed / fallback-queued).  Registered at startup by
# main.py so that the discovery service can immediately scan for the next file.
_on_job_complete: Callable[[], None] | None = None


def register_job_complete_callback(fn: Callable[[], None]) -> None:
    global _on_job_complete
    _on_job_complete = fn


# ─────────────────────────────────────────────────────────────────────────────
# Thread lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def start_download_monitor() -> threading.Thread:
    global _thread
    if _thread and _thread.is_alive():
        return _thread

    _stop_event.clear()
    _wake_event.clear()
    _thread = threading.Thread(target=_monitor_loop, name='download-monitor', daemon=True)
    _thread.start()
    logger.info('Download monitor worker started')
    return _thread


def stop_download_monitor() -> None:
    _stop_event.set()
    _wake_event.set()  # unblock any pending wait immediately


def _monitor_loop() -> None:
    global _last_idle_recovery_monotonic
    while not _stop_event.is_set():
        db = SessionLocal()
        sleep_seconds = _POLL_IDLE_SECONDS
        try:
            has_downloading = (
                db.query(DownloadJob)
                .filter(DownloadJob.status.in_([
                    DownloadJobStatus.queued.value,
                    DownloadJobStatus.downloading.value,
                    DownloadJobStatus.moving.value,
                ]))
                .count()
            ) > 0
            has_searching = (
                db.query(DownloadJob)
                .filter(DownloadJob.status == DownloadJobStatus.searching.value)
                .count()
            ) > 0

            _process_searching_jobs(db)
            _process_downloading_jobs(db)

            # Safety net: when the pipeline is otherwise idle, periodically
            # reconcile non-active download jobs so completed qBit items that
            # finished after a transient stall/not-found state are imported.
            if not has_downloading and not has_searching and not _download_queue_stopped:
                now_monotonic = time.monotonic()
                if now_monotonic - _last_idle_recovery_monotonic >= _IDLE_RECOVERY_INTERVAL_SECONDS:
                    run_scan_recovery(db)
                    _link_completed_downloads_to_waiting_jobs(db)
                    _last_idle_recovery_monotonic = now_monotonic

            if has_downloading:
                sleep_seconds = _POLL_DOWNLOADING_SECONDS
            elif has_searching:
                sleep_seconds = _POLL_SEARCHING_SECONDS
        except Exception:
            logger.exception('Download monitor iteration failed')
        finally:
            db.close()

        # Wait for the computed interval OR until woken by a new job / shutdown
        _wake_event.wait(timeout=sleep_seconds)
        _wake_event.clear()

    logger.info('Download monitor worker stopped')


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers used by discovery_service
# ─────────────────────────────────────────────────────────────────────────────

def can_attempt_download(db: Session) -> bool:
    """Return True if Prowlarr is enabled and at least one download client is enabled."""
    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    if not prowlarr.enabled:
        return False
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)
    return bool(qbt.enabled or sab.enabled)


def download_job_exists_for_source(db: Session, source_path: str, library_id: int | None = None) -> bool:
    active_statuses = {
        DownloadJobStatus.pending.value,
        DownloadJobStatus.searching.value,
        DownloadJobStatus.queued.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
        DownloadJobStatus.waiting_encode.value,
        DownloadJobStatus.complete.value,
    }
    rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.source_file_path == source_path,
            DownloadJob.status.in_(active_statuses),
        )
        .all()
    )
    for dj in rows:
        if dj.status != DownloadJobStatus.complete.value:
            return True
        imported = Path(dj.imported_file_path) if dj.imported_file_path else None
        if imported and imported.exists():
            return True

    identity_key = media_identity_key(source_path)
    if not identity_key or library_id is None:
        return False

    identity_rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.library_id == library_id,
            DownloadJob.source_file_path != source_path,
            DownloadJob.status.in_(active_statuses),
        )
        .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
        .all()
    )
    for dj in identity_rows:
        if media_identity_key(dj.source_file_path) != identity_key:
            continue
        if dj.status != DownloadJobStatus.complete.value:
            return True
        imported = Path(dj.imported_file_path) if dj.imported_file_path else None
        if imported and imported.exists():
            return True
    return False


_IDENTITY_BLOCKING_DOWNLOAD_STATUSES = {
    DownloadJobStatus.searching.value,
    DownloadJobStatus.queued.value,
    DownloadJobStatus.downloading.value,
    DownloadJobStatus.moving.value,
    DownloadJobStatus.stalled.value,
    DownloadJobStatus.importing.value,
    DownloadJobStatus.waiting_encode.value,
    DownloadJobStatus.complete.value,
}


def _download_job_identity_blocker(db: Session, dj: DownloadJob) -> DownloadJob | None:
    source_path = str(dj.source_file_path or '')
    identity_key = media_identity_key(source_path)
    rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.id != dj.id,
            DownloadJob.status.in_(_IDENTITY_BLOCKING_DOWNLOAD_STATUSES),
        )
        .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
        .all()
    )
    for other in rows:
        same_identity = False
        if source_path and other.source_file_path == source_path:
            same_identity = True
        elif identity_key:
            same_identity = media_identity_key(other.source_file_path) == identity_key
        if not same_identity:
            continue

        if other.status != DownloadJobStatus.complete.value:
            return other
        imported = Path(other.imported_file_path) if other.imported_file_path else None
        if imported and imported.exists():
            return other
    return None


def _remove_duplicate_unstarted_download_job(db: Session, dj: DownloadJob, blocker: DownloadJob) -> None:
    job_id = dj.id
    logger.info(
        'Download job %s removed as duplicate of active/completed download job %s for %r',
        job_id,
        blocker.id,
        dj.source_file_path,
    )
    db.delete(dj)
    db.commit()
    broker.publish_system_event('download_job_removed', download_job_id=job_id)


def _release_identity_key(value: str | None) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    return media_identity_key(f'/downloads/{raw}.mkv')


def _tv_download_identity_key(value: str | None) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    season, episode = _extract_season_episode(raw)
    if season is None or episode is None:
        return None

    stem = Path(raw).stem
    clean = re.sub(r'[._-]+', ' ', stem)
    match = re.search(r'\bs\d{1,2}e\d{1,3}\b|\b\d{1,2}x\d{1,3}\b', clean, flags=re.IGNORECASE)
    title_part = clean[:match.start()] if match else clean
    title_part = re.sub(r'\b(?:19|20)\d{2}\b', ' ', title_part)
    tokens = [
        token
        for token in re.findall(r'[a-z0-9]+', title_part.lower())
        if len(token) > 1 and token not in {'the', 'and', 'season', 'series', 'downloads'}
    ]
    if not tokens:
        return None
    return f'tvrel:{" ".join(tokens[:6])}:s{season:02d}:e{episode:03d}'


def _loose_download_identity_key(value: str | None) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    if _extract_season_episode(raw) != (None, None) or _is_probable_tv_episode_title(raw):
        return None
    year = _extract_year_from_path(raw)
    if year is None:
        return None
    tokens = _extract_title_tokens(raw)
    if not tokens:
        return None
    return f'loose:{year}:{tokens[0]}'


def _download_identity_keys(value: str | None) -> set[str]:
    return {
        key
        for key in (
            _tv_download_identity_key(value),
            media_identity_key(value),
            _release_identity_key(value),
            _loose_download_identity_key(value),
        )
        if key
    }


def _torrent_identity_key(torrent: dict) -> str | None:
    name = str(torrent.get('name') or '')
    return _tv_download_identity_key(name) or _loose_download_identity_key(name) or _release_identity_key(name) or media_identity_key(name)


def _qbt_torrent_progress(torrent: dict) -> float:
    try:
        return float(torrent.get('progress') or 0)
    except (TypeError, ValueError):
        return 0.0


def _qbt_torrent_added_on(torrent: dict) -> int:
    try:
        return int(torrent.get('added_on') or 0)
    except (TypeError, ValueError):
        return 0


def _is_incomplete_qbt_torrent(torrent: dict) -> bool:
    state = str(torrent.get('state') or '').strip()
    return state not in download_client_service._QBT_COMPLETE_STATES and _qbt_torrent_progress(torrent) < 1.0


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(float(raw_value))
    except ValueError:
        logger.warning('Invalid %s=%r; using %d', name, raw_value, default)
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _qbt_settings_int(settings: Settings | None, attr_name: str, env_name: str, default: int, *, minimum: int = 0) -> int:
    if settings is not None:
        value = getattr(settings, attr_name, None)
        if value is not None:
            try:
                return max(minimum, int(value))
            except (TypeError, ValueError):
                pass
    return _env_int(env_name, default, minimum=minimum)


def _qbt_settings_bool(settings: Settings | None, attr_name: str, env_name: str, default: bool) -> bool:
    if settings is not None:
        value = getattr(settings, attr_name, None)
        if value is not None:
            return bool(value)
    return _env_bool(env_name, default)


def _qbt_torrent_has_optimizarr_ownership(torrent: dict) -> bool:
    tag_values = str(torrent.get('tags') or '').split(',')
    category = str(torrent.get('category') or '').strip().lower()
    return (
        download_client_service._QBT_TAG in {tag.strip().lower() for tag in tag_values}
        or category == download_client_service._QBT_TAG
    )


def _qbt_torrent_is_private(torrent: dict) -> bool:
    raw_value = torrent.get('private')
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return raw_value > 0
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {'1', 'true', 'yes', 'private'}
    return False


def _active_qbt_download_jobs_by_hash(db: Session) -> dict[str, DownloadJob]:
    active_statuses = {
        DownloadJobStatus.queued.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
    }
    rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.client_type == 'qbittorrent',
            DownloadJob.download_hash.is_not(None),
            DownloadJob.status.in_(active_statuses),
        )
        .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
        .all()
    )
    jobs_by_hash: dict[str, DownloadJob] = {}
    for row in rows:
        torrent_hash = str(row.download_hash or '').strip().lower()
        if torrent_hash and torrent_hash not in jobs_by_hash:
            jobs_by_hash[torrent_hash] = row
    return jobs_by_hash


def _qbt_torrent_download_speed(torrent: dict) -> int:
    try:
        return max(0, int(float(torrent.get('dlspeed') or 0)))
    except (TypeError, ValueError):
        return 0


def _qbt_torrent_strike_reason(torrent: dict, settings: Settings | None = None) -> tuple[str, int, bool] | None:
    if not _qbt_torrent_has_optimizarr_ownership(torrent):
        return None
    state = str(torrent.get('state') or '').strip()
    if state in _QBT_METADATA_STATES:
        max_strikes = _qbt_settings_int(
            settings,
            'qbt_metadata_max_strikes',
            'OPTIMIZARR_QBT_METADATA_MAX_STRIKES',
            _QBT_METADATA_MAX_STRIKES,
        )
        if max_strikes <= 0:
            return None
        return ('stuck downloading metadata', max_strikes, False)

    if state in _QBT_STALE_STATES:
        max_strikes = _qbt_settings_int(
            settings,
            'qbt_stalled_max_strikes',
            'OPTIMIZARR_QBT_STALLED_MAX_STRIKES',
            _QBT_STALLED_MAX_STRIKES,
        )
        if max_strikes <= 0:
            return None
        return (f'stalled qBittorrent state {state}', max_strikes, True)

    min_speed_bps = _qbt_settings_int(
        settings,
        'qbt_slow_min_speed_bps',
        'OPTIMIZARR_QBT_SLOW_MIN_SPEED_BPS',
        _QBT_SLOW_MIN_SPEED_BPS,
    )
    if min_speed_bps <= 0:
        return None
    if _qbt_settings_bool(settings, 'qbt_slow_ignore_private', 'OPTIMIZARR_QBT_SLOW_IGNORE_PRIVATE', True) and _qbt_torrent_is_private(torrent):
        return None
    if not _is_incomplete_qbt_torrent(torrent):
        return None
    download_speed_bps = _qbt_torrent_download_speed(torrent)
    if download_speed_bps >= min_speed_bps:
        return None
    max_strikes = _qbt_settings_int(
        settings,
        'qbt_slow_max_strikes',
        'OPTIMIZARR_QBT_SLOW_MAX_STRIKES',
        _QBT_SLOW_MAX_STRIKES,
    )
    if max_strikes <= 0:
        return None
    return (f'slow download below {min_speed_bps} B/s', max_strikes, True)


def _reset_qbt_strike(torrent_hash: str) -> None:
    if torrent_hash:
        _qbt_strike_state.pop(torrent_hash, None)


def _record_qbt_strike(torrent_hash: str, reason: str) -> int:
    state = _qbt_strike_state.get(torrent_hash)
    if not state or state.get('reason') != reason:
        state = {'reason': reason, 'strikes': 0}
    strikes = int(state.get('strikes') or 0) + 1
    state['strikes'] = strikes
    _qbt_strike_state[torrent_hash] = state
    return strikes


def _retry_qbt_download_job_after_strikes(db: Session, dj: DownloadJob, reason: str) -> None:
    library = db.query(Library).filter(Library.id == dj.library_id).first()
    if library is None or library.profile is None:
        _mark_failed(db, dj, f'{reason}; library or profile not found')
        return
    _retry_failed_download(
        db,
        dj,
        library,
        library.profile,
        reason=reason,
        failed_release_key=_release_selection_key_from_job(dj),
    )


def _cleanup_stale_qbt_torrents(db: Session, qbt, *, force: bool = False) -> int:
    global _last_qbt_strike_cleanup_monotonic
    if not getattr(qbt, 'enabled', False):
        return 0

    settings = db.query(Settings).filter(Settings.id == 1).first()
    interval_seconds = _qbt_settings_int(
        settings,
        'qbt_strike_check_interval_seconds',
        'OPTIMIZARR_QBT_STRIKE_CHECK_INTERVAL_SECONDS',
        _QBT_STRIKE_CHECK_INTERVAL_SECONDS,
        minimum=1,
    )
    now_monotonic = time.monotonic()
    if not force and now_monotonic - _last_qbt_strike_cleanup_monotonic < interval_seconds:
        return 0
    _last_qbt_strike_cleanup_monotonic = now_monotonic

    active_jobs_by_hash = _active_qbt_download_jobs_by_hash(db)
    seen_hashes: set[str] = set()
    removed = 0
    for torrent in download_client_service.get_all_qbt_torrents(qbt):
        torrent_hash = str(torrent.get('hash') or '').strip().lower()
        if not torrent_hash:
            continue
        seen_hashes.add(torrent_hash)
        strike_rule = _qbt_torrent_strike_reason(torrent, settings)
        if not strike_rule:
            _reset_qbt_strike(torrent_hash)
            continue
        reason, max_strikes, delete_files = strike_rule
        strikes = _record_qbt_strike(torrent_hash, reason)
        logger.warning(
            'qBittorrent strike %s/%s for Optimizarr torrent: hash=%s name=%r reason=%s private=%s',
            strikes,
            max_strikes,
            torrent_hash,
            torrent.get('name'),
            reason,
            _qbt_torrent_is_private(torrent),
        )
        if strikes < max_strikes:
            continue

        if download_client_service.remove_qbt_torrent(qbt, torrent_hash, delete_files=delete_files):
            removed += 1
            _reset_qbt_strike(torrent_hash)
            logger.warning(
                'Removed Optimizarr qBittorrent torrent after %s strikes: hash=%s name=%r delete_files=%s reason=%s',
                max_strikes,
                torrent_hash,
                torrent.get('name'),
                delete_files,
                reason,
            )
            tracked_job = active_jobs_by_hash.get(torrent_hash)
            if tracked_job is not None:
                _retry_qbt_download_job_after_strikes(db, tracked_job, reason)

    for stale_hash in set(_qbt_strike_state) - seen_hashes:
        _reset_qbt_strike(stale_hash)
    return removed


def _active_download_identity_keys(db: Session) -> set[str]:
    active_statuses = {
        DownloadJobStatus.searching.value,
        DownloadJobStatus.queued.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
    }
    keys: set[str] = set()
    rows = db.query(DownloadJob).filter(DownloadJob.status.in_(active_statuses)).all()
    for dj in rows:
        for value in (dj.source_file_path, dj.release_name):
            keys.update(_download_identity_keys(value))
    return keys


def _tracked_qbt_hashes_by_identity(db: Session) -> dict[str, set[str]]:
    hashes_by_key: dict[str, set[str]] = {}
    rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.client_type == 'qbittorrent',
            DownloadJob.download_hash.is_not(None),
            DownloadJob.status.in_({
                DownloadJobStatus.queued.value,
                DownloadJobStatus.downloading.value,
                DownloadJobStatus.moving.value,
                DownloadJobStatus.stalled.value,
                DownloadJobStatus.importing.value,
            }),
        )
        .all()
    )
    for dj in rows:
        torrent_hash = str(dj.download_hash or '').strip().lower()
        if not torrent_hash:
            continue
        for key in _download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name):
            hashes_by_key.setdefault(key, set()).add(torrent_hash)
    return hashes_by_key


def _active_download_jobs_for_identity(db: Session, identity_key: str) -> list[DownloadJob]:
    active_statuses = {
        DownloadJobStatus.searching.value,
        DownloadJobStatus.queued.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
    }
    rows = (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_(active_statuses))
        .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
        .all()
    )
    matches: list[DownloadJob] = []
    for dj in rows:
        keys = _download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name)
        if identity_key in keys:
            matches.append(dj)
    return matches


def _retarget_download_jobs_to_client_item(
    db: Session,
    identity_key: str,
    *,
    client_type: str,
    download_hash: str,
) -> None:
    if not download_hash:
        return
    jobs = _active_download_jobs_for_identity(db, identity_key)
    if not jobs:
        return

    primary = jobs[0]
    changed = False
    if primary.client_type != client_type:
        primary.client_type = client_type
        changed = True
    if primary.download_hash != download_hash:
        primary.download_hash = download_hash
        changed = True
    if primary.status != DownloadJobStatus.downloading.value:
        primary.status = DownloadJobStatus.downloading.value
        changed = True
    primary.error_message = None

    for duplicate in jobs[1:]:
        if duplicate.download_hash == download_hash:
            continue
        duplicate.download_hash = None
        duplicate.status = DownloadJobStatus.searching.value
        duplicate.error_message = 'Superseded by active client download'
        changed = True

    if changed:
        db.commit()
        for job in jobs:
            db.refresh(job)
            _publish_download_job(job)


def _tracked_sab_nzos_by_identity(db: Session) -> dict[str, set[str]]:
    nzos_by_key: dict[str, set[str]] = {}
    rows = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.client_type == 'sabnzbd',
            DownloadJob.download_hash.is_not(None),
            DownloadJob.status.in_({
                DownloadJobStatus.queued.value,
                DownloadJobStatus.downloading.value,
                DownloadJobStatus.moving.value,
                DownloadJobStatus.stalled.value,
                DownloadJobStatus.importing.value,
            }),
        )
        .all()
    )
    for dj in rows:
        nzo_id = str(dj.download_hash or '').strip()
        if not nzo_id:
            continue
        for key in _download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name):
            nzos_by_key.setdefault(key, set()).add(nzo_id)
    return nzos_by_key


def _reconcile_duplicate_qbt_downloads(db: Session, qbt) -> int:
    if not getattr(qbt, 'enabled', False):
        return 0

    active_keys = _active_download_identity_keys(db)
    if not active_keys:
        return 0

    torrents_by_key: dict[str, list[dict]] = {}
    for torrent in download_client_service.get_all_qbt_torrents(qbt):
        torrent_hash = str(torrent.get('hash') or '').strip().lower()
        if not torrent_hash or not _is_incomplete_qbt_torrent(torrent):
            continue
        key = _torrent_identity_key(torrent)
        if key not in active_keys:
            continue
        torrents_by_key.setdefault(key, []).append(torrent)

    tracked_hashes = _tracked_qbt_hashes_by_identity(db)
    removed = 0
    for key, torrents in torrents_by_key.items():
        if len(torrents) <= 1:
            continue
        tracked = tracked_hashes.get(key, set())

        def keep_score(torrent: dict) -> tuple[int, float, int]:
            torrent_hash = str(torrent.get('hash') or '').strip().lower()
            return (
                _qbt_torrent_progress(torrent),
                1 if torrent_hash in tracked else 0,
                -_qbt_torrent_added_on(torrent),
            )

        keep = max(torrents, key=keep_score)
        keep_hash = str(keep.get('hash') or '').strip().lower()
        _retarget_download_jobs_to_client_item(
            db,
            key,
            client_type='qbittorrent',
            download_hash=keep_hash,
        )
        for torrent in torrents:
            torrent_hash = str(torrent.get('hash') or '').strip().lower()
            if not torrent_hash or torrent_hash == keep_hash:
                continue
            if download_client_service.remove_qbt_torrent(qbt, torrent_hash, delete_files=True):
                removed += 1
                logger.warning(
                    'Removed duplicate qBittorrent alternative for %s: hash=%s name=%r; keeping hash=%s',
                    key,
                    torrent_hash,
                    torrent.get('name'),
                    keep_hash,
                )
                duplicate_rows = (
                    db.query(DownloadJob)
                    .filter(
                        DownloadJob.client_type == 'qbittorrent',
                        DownloadJob.download_hash == torrent_hash,
                    )
                    .all()
                )
                for duplicate in duplicate_rows:
                    duplicate.download_hash = None
                    duplicate.status = DownloadJobStatus.searching.value
                    duplicate.error_message = 'Removed duplicate download alternative; retrying search'
                if duplicate_rows:
                    db.commit()
                    for duplicate in duplicate_rows:
                        db.refresh(duplicate)
                        _publish_download_job(duplicate)
    return removed


def _reconcile_duplicate_sab_downloads(db: Session, sab) -> int:
    if not getattr(sab, 'enabled', False):
        return 0

    active_keys = _active_download_identity_keys(db)
    if not active_keys:
        return 0

    items_by_key: dict[str, list[dict]] = {}
    for item in download_client_service.get_sab_queue_items(sab):
        nzo_id = str(item.get('nzo_id') or '').strip()
        if not nzo_id:
            continue
        name = str(item.get('name') or '')
        key = _tv_download_identity_key(name) or _loose_download_identity_key(name) or _release_identity_key(name) or media_identity_key(name)
        if key not in active_keys:
            continue
        items_by_key.setdefault(key, []).append(item)

    tracked_nzos = _tracked_sab_nzos_by_identity(db)
    removed = 0
    for key, items in items_by_key.items():
        if len(items) <= 1:
            continue
        tracked = tracked_nzos.get(key, set())

        def keep_score(item: dict) -> tuple[int, float, int]:
            nzo_id = str(item.get('nzo_id') or '').strip()
            try:
                percentage = float(item.get('percentage') or 0)
            except (TypeError, ValueError):
                percentage = 0.0
            try:
                index = int(item.get('index') or 0)
            except (TypeError, ValueError):
                index = 0
            return (
                percentage,
                1 if nzo_id in tracked else 0,
                -index,
            )

        keep = max(items, key=keep_score)
        keep_nzo = str(keep.get('nzo_id') or '').strip()
        _retarget_download_jobs_to_client_item(
            db,
            key,
            client_type='sabnzbd',
            download_hash=keep_nzo,
        )
        for item in items:
            nzo_id = str(item.get('nzo_id') or '').strip()
            if not nzo_id or nzo_id == keep_nzo:
                continue
            if download_client_service.remove_sab_job(sab, nzo_id, delete_files=True):
                removed += 1
                logger.warning(
                    'Removed duplicate SABnzbd alternative for %s: nzo=%s name=%r; keeping nzo=%s',
                    key,
                    nzo_id,
                    item.get('name'),
                    keep_nzo,
                )
                duplicate_rows = (
                    db.query(DownloadJob)
                    .filter(
                        DownloadJob.client_type == 'sabnzbd',
                        DownloadJob.download_hash == nzo_id,
                    )
                    .all()
                )
                for duplicate in duplicate_rows:
                    duplicate.download_hash = None
                    duplicate.status = DownloadJobStatus.searching.value
                    duplicate.error_message = 'Removed duplicate download alternative; retrying search'
                if duplicate_rows:
                    db.commit()
                    for duplicate in duplicate_rows:
                        db.refresh(duplicate)
                        _publish_download_job(duplicate)
    return removed


def _find_sab_queue_item_for_download_job(dj: DownloadJob, sab) -> dict | None:
    if not getattr(sab, 'enabled', False):
        return None
    target_keys = _download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name)
    if not target_keys:
        return None

    candidates: list[dict] = []
    for item in download_client_service.get_sab_queue_items(sab):
        name = str(item.get('name') or '')
        item_keys = _download_identity_keys(name)
        if target_keys.isdisjoint(item_keys):
            continue
        candidates.append(item)

    if not candidates:
        return None

    def score(item: dict) -> tuple[float, int]:
        try:
            percentage = float(item.get('percentage') or 0)
        except (TypeError, ValueError):
            percentage = 0.0
        try:
            index = int(item.get('index') or 0)
        except (TypeError, ValueError):
            index = 0
        return (percentage, -index)

    return max(candidates, key=score)


def _sab_queue_item_for_nzo(sab, nzo_id: str | None) -> dict | None:
    target_nzo = str(nzo_id or '').strip()
    if not target_nzo or not getattr(sab, 'enabled', False):
        return None
    for item in download_client_service.get_sab_queue_items(sab):
        if str(item.get('nzo_id') or '').strip() == target_nzo:
            return item
    return None


def _qbt_torrent_for_hash(qbt, torrent_hash: str | None) -> dict | None:
    target_hash = str(torrent_hash or '').strip().lower()
    if not target_hash or not getattr(qbt, 'enabled', False):
        return None
    for torrent in download_client_service.get_all_qbt_torrents(qbt):
        if str(torrent.get('hash') or '').strip().lower() == target_hash:
            return torrent
    return None


def _tv_identity_keys(keys: set[str]) -> set[str]:
    return {key for key in keys if key.startswith('tv:') or key.startswith('tvrel:')}


def _sab_nzo_mismatches_tv_download_job(dj: DownloadJob, sab) -> bool:
    if str(dj.client_type or '').lower() != 'sabnzbd' or not dj.download_hash:
        return False
    item = _sab_queue_item_for_nzo(sab, dj.download_hash)
    if not item:
        return False
    target_tv_keys = _tv_identity_keys(_download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name))
    if not target_tv_keys:
        return False
    item_tv_keys = _tv_identity_keys(_download_identity_keys(str(item.get('name') or '')))
    return bool(item_tv_keys and target_tv_keys.isdisjoint(item_tv_keys))


def _qbt_hash_mismatches_tv_download_job(dj: DownloadJob, qbt) -> bool:
    if str(dj.client_type or '').lower() != 'qbittorrent' or not dj.download_hash:
        return False
    torrent = _qbt_torrent_for_hash(qbt, dj.download_hash)
    if not torrent:
        return False
    target_tv_keys = _tv_identity_keys(_download_identity_keys(dj.source_file_path) | _download_identity_keys(dj.release_name))
    if not target_tv_keys:
        return False
    torrent_tv_keys = _tv_identity_keys(_download_identity_keys(str(torrent.get('name') or '')))
    return bool(torrent_tv_keys and target_tv_keys.isdisjoint(torrent_tv_keys))


_ACTIVE_DOWNLOAD_STATUSES = (
    DownloadJobStatus.pending.value,
    DownloadJobStatus.searching.value,
    DownloadJobStatus.queued.value,
    DownloadJobStatus.downloading.value,
    DownloadJobStatus.moving.value,
    DownloadJobStatus.importing.value,
)


def any_active_download_job(db: Session) -> bool:
    """Return True if any download job is currently searching, downloading, moving, or importing."""
    return db.query(
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_(_ACTIVE_DOWNLOAD_STATUSES))
        .exists()
    ).scalar()


def create_download_job(db: Session, source_path: str, library: Library, profile: LibraryProfile) -> DownloadJob:
    if recover_completed_artifact_for_source(db, source_path, library, profile):
        existing = (
            db.query(DownloadJob)
            .filter(
                DownloadJob.library_id == library.id,
                DownloadJob.source_file_path == source_path,
            )
            .order_by(DownloadJob.id.desc())
            .first()
        )
        if existing is not None:
            return existing

    existing = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.library_id == library.id,
            DownloadJob.status.in_({
                DownloadJobStatus.pending.value,
                DownloadJobStatus.searching.value,
                DownloadJobStatus.queued.value,
                DownloadJobStatus.downloading.value,
                DownloadJobStatus.moving.value,
                DownloadJobStatus.stalled.value,
                DownloadJobStatus.importing.value,
                DownloadJobStatus.waiting_encode.value,
                DownloadJobStatus.complete.value,
            }),
        )
        .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
        .all()
    )
    identity_key = media_identity_key(source_path)
    if identity_key:
        for dj in existing:
            if media_identity_key(dj.source_file_path) == identity_key:
                if dj.status != DownloadJobStatus.complete.value:
                    return dj
                imported = Path(dj.imported_file_path) if dj.imported_file_path else None
                if imported and imported.exists():
                    return dj

    dj = DownloadJob(
        library_id=library.id,
        source_file_path=source_path,
        status=DownloadJobStatus.pending.value,
        retry_count=0,
        max_retries=_DEFAULT_DOWNLOAD_MAX_RETRIES,
    )
    db.add(dj)
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    logger.info('Download job %s created for %r', dj.id, source_path)
    # Wake the monitor loop immediately so the Prowlarr search fires now
    _wake_event.set()
    return dj


def download_job_to_dict(dj: DownloadJob) -> dict:
    def _iso_utc(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()

    return {
        'id': dj.id,
        'library_id': dj.library_id,
        'source_file_path': dj.source_file_path,
        'search_query': dj.search_query,
        'release_name': dj.release_name,
        'indexer_id': dj.indexer_id,
        'indexer_name': dj.indexer_name,
        'selected_release_key': dj.selected_release_key,
        'failed_release_keys': dj.failed_release_keys,
        'retry_count': dj.retry_count,
        'max_retries': dj.max_retries,
        'download_hash': dj.download_hash,
        'client_type': dj.client_type,
        'status': dj.status,
        'progress_percent': dj.progress_percent,
        'eta_seconds': dj.eta_seconds,
        'download_speed_bps': dj.download_speed_bps,
        'downloaded_file_path': dj.downloaded_file_path,
        'imported_file_path': dj.imported_file_path,
        'error_message': dj.error_message,
        'encode_job_id': dj.encode_job_id,
        'created_at': _iso_utc(dj.created_at),
        'download_started_at': _iso_utc(dj.download_started_at),
        'completed_at': _iso_utc(dj.completed_at),
    }


def _publish_download_job(dj: DownloadJob) -> None:
    broker.publish('download_job_update', download_job_to_dict(dj))


def _link_completed_downloads_to_waiting_jobs(db: Session) -> int:
    """Remove waiting encode placeholders once the download import is complete.

    Download-enabled libraries create an encode queue row as an orchestration
    placeholder. When the download path succeeds and the file is imported, that
    placeholder should disappear rather than creating an "encoded complete"
    history record.
    """
    terminal_waiting_statuses = ('queued', 'paused')
    active_encode_statuses = ('starting', 'preflight', 'running', 'aborting')
    completed_downloads = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.status == DownloadJobStatus.complete.value,
            DownloadJob.imported_file_path.isnot(None),
        )
        .all()
    )
    if not completed_downloads:
        return 0

    removed_job_ids: list[int] = []
    cancel_requested_job_ids: list[int] = []
    for dj in completed_downloads:
        waiting_jobs = (
            db.query(Job)
            .filter(
                Job.input_path == dj.source_file_path,
                Job.library_id == dj.library_id,
                Job.status.in_(terminal_waiting_statuses),
            )
            .all()
        )
        for job in waiting_jobs:
            removed_job_ids.append(job.id)
            db.delete(job)
        active_jobs = (
            db.query(Job)
            .filter(
                Job.input_path == dj.source_file_path,
                Job.library_id == dj.library_id,
                Job.status.in_(active_encode_statuses),
            )
            .all()
        )
        for job in active_jobs:
            stop_active_ffmpeg(job.id)
            job.cancel_requested = True
            job.error_message = 'Cancelled: completed download imported'
            if job.status in {'starting', 'preflight', 'running'}:
                job.status = 'aborting'
                job.completed_at = None
            cancel_requested_job_ids.append(job.id)

    if not removed_job_ids and not cancel_requested_job_ids:
        return 0

    db.commit()
    for job_id in removed_job_ids:
        broker.publish_system_event('job_removed', job_id=job_id)
    for job_id in cancel_requested_job_ids:
        broker.publish_system_event('job_aborted', job_id=job_id)
    logger.info(
        'Resolved completed-download placeholder conflicts: removed=%s cancel_requested=%s',
        len(removed_job_ids),
        len(cancel_requested_job_ids),
    )
    return len(removed_job_ids) + len(cancel_requested_job_ids)


def _recover_completed_root_for_waiting_queue_jobs(
    db: Session,
    qbt,
    sab,
    qbt_completed_root: str | None,
) -> int:
    """Adopt completed qBit artifacts for queued download-enabled encode jobs.

    This handles restarts/data-gaps where an encode Job exists but its DownloadJob
    row is missing or never persisted.
    """
    if not getattr(qbt, 'enabled', False) or not qbt_completed_root:
        return 0

    waiting_statuses = ('queued', 'paused', 'starting', 'preflight')
    queue_rows = (
        db.query(Job, Library, LibraryProfile)
        .join(Library, Job.library_id == Library.id)
        .join(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(
            Job.status.in_(waiting_statuses),
            LibraryProfile.download_enabled.is_(True),
        )
        .all()
    )

    adopted = 0
    for job, library, profile in queue_rows:
        if not job.source_path:
            continue
        if download_job_exists_for_source(db, job.source_path):
            logger.info(
                'Queue adoption: skipping source %r because an active/complete download job already exists',
                job.source_path,
            )
            continue

        probe = DownloadJob(
            library_id=library.id,
            source_file_path=job.source_path,
            release_name=None,
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        completed_match = _find_completed_download_match(probe, qbt_completed_root)
        if not completed_match:
            continue

        completed_name = Path(completed_match).name
        if not _release_title_matches_profile(completed_name, profile):
            logger.info(
                'Queue adoption: source %r matched completed path %r but failed profile check',
                job.source_path,
                completed_name,
            )
            continue

        dj = DownloadJob(
            library_id=library.id,
            source_file_path=job.source_path,
            release_name=completed_name,
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        logger.info(
            'Queue adoption: created download job %s for queued job %s from completed path %r',
            dj.id,
            job.id,
            completed_match,
        )
        _import_file(db, dj, completed_match, library, profile, qbt, sab)
        adopted += 1

    if adopted:
        logger.info('Queue adoption: imported %s completed artifact(s) for waiting queue jobs', adopted)
    return adopted


def recover_completed_artifact_for_source(
    db: Session,
    source_path: str,
    library: Library | None,
    profile: LibraryProfile | None,
    *,
    queue_job_id: int | None = None,
) -> bool:
    """Import a completed client artifact for a single source if present."""
    if library is None or profile is None or not getattr(profile, 'download_enabled', False):
        return False
    if not source_path:
        return False
    if download_job_exists_for_source(db, source_path):
        return False

    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)

    if getattr(qbt, 'enabled', False):
        qbt_completed_root = download_client_service.get_qbt_default_save_path(qbt)
        if qbt_completed_root:
            probe = DownloadJob(
                library_id=library.id,
                source_file_path=source_path,
                release_name=None,
                client_type='qbittorrent',
                status=DownloadJobStatus.downloading.value,
            )
            completed_match = _find_completed_download_match(probe, qbt_completed_root)
            if completed_match:
                completed_name = Path(completed_match).name
                if _release_title_matches_profile(completed_name, profile):
                    dj = DownloadJob(
                        library_id=library.id,
                        source_file_path=source_path,
                        release_name=completed_name,
                        client_type='qbittorrent',
                        status=DownloadJobStatus.downloading.value,
                    )
                    db.add(dj)
                    db.commit()
                    db.refresh(dj)
                    logger.info(
                        'Completed-artifact recovery: created download job %s for source %r'
                        ' (queue_job_id=%s) from completed path %r',
                        dj.id,
                        source_path,
                        queue_job_id,
                        completed_match,
                    )
                    _import_file(db, dj, completed_match, library, profile, qbt, sab)
                    return True
                logger.info(
                    'Completed-artifact recovery: source %r matched completed path %r but failed profile check',
                    source_path,
                    completed_name,
                )

    if getattr(sab, 'enabled', False):
        probe = DownloadJob(
            library_id=library.id,
            source_file_path=source_path,
            release_name=None,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
        )
        keys = _build_completed_root_match_keys(probe)
        if not keys:
            return False

        completed_items = download_client_service.get_sab_completed_history_items(sab)
        for item in completed_items:
            name = str(item.get('name') or '')
            name_key = _normalize_release_key(name)
            if not name_key:
                continue
            if not any(k and (k in name_key or name_key in k) for k in keys):
                continue
            if not _release_title_matches_profile(name, profile):
                logger.info(
                    'Completed-artifact recovery: source %r matched completed SAB history %r but failed profile check',
                    source_path,
                    name,
                )
                return False

            save_path = str(item.get('save_path') or '').strip()
            nzo_id = str(item.get('nzo_id') or '').strip()
            if not save_path or not nzo_id:
                return False

            dj = DownloadJob(
                library_id=library.id,
                source_file_path=source_path,
                release_name=name,
                download_hash=nzo_id,
                client_type='sabnzbd',
                status=DownloadJobStatus.downloading.value,
            )
            db.add(dj)
            db.commit()
            db.refresh(dj)
            logger.info(
                'Completed-artifact recovery: created download job %s for source %r'
                ' (queue_job_id=%s) from SAB history %r',
                dj.id,
                source_path,
                queue_job_id,
                name,
            )
            _import_file(db, dj, save_path, library, profile, qbt, sab)
            return True

    return False


def recover_completed_artifact_for_queue_job(
    db: Session,
    job: Job,
    library: Library | None,
    profile: LibraryProfile | None,
) -> bool:
    """Import a completed client artifact for a single queued download-enabled job."""
    return recover_completed_artifact_for_source(
        db,
        job.source_path or '',
        library,
        profile,
        queue_job_id=job.id,
    )


def _recover_sab_completed_for_waiting_queue_jobs(
    db: Session,
    qbt,
    sab,
) -> int:
    """Adopt completed SAB history entries for queued download-enabled encode jobs."""
    if not getattr(sab, 'enabled', False):
        return 0

    completed_items = download_client_service.get_sab_completed_history_items(sab)
    if not completed_items:
        return 0

    waiting_statuses = ('queued', 'paused', 'starting', 'preflight')
    queue_rows = (
        db.query(Job, Library, LibraryProfile)
        .join(Library, Job.library_id == Library.id)
        .join(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(
            Job.status.in_(waiting_statuses),
            LibraryProfile.download_enabled.is_(True),
        )
        .all()
    )

    adopted = 0
    for job, library, profile in queue_rows:
        if not job.source_path:
            continue
        if download_job_exists_for_source(db, job.source_path):
            logger.info(
                'SAB queue adoption: skipping source %r because an active/complete download job already exists',
                job.source_path,
            )
            continue

        probe = DownloadJob(
            library_id=library.id,
            source_file_path=job.source_path,
            release_name=None,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
        )
        keys = _build_completed_root_match_keys(probe)
        if not keys:
            continue

        matched_item = None
        for item in completed_items:
            name = str(item.get('name') or '')
            name_key = _normalize_release_key(name)
            if not name_key:
                continue
            if any(k and (k in name_key or name_key in k) for k in keys):
                matched_item = item
                break
        if not matched_item:
            continue

        matched_name = str(matched_item.get('name') or '')
        if not _release_title_matches_profile(matched_name, profile):
            logger.info(
                'SAB queue adoption: source %r matched completed history %r but failed profile check',
                job.source_path,
                matched_name,
            )
            continue

        save_path = str(matched_item.get('save_path') or '').strip()
        nzo_id = str(matched_item.get('nzo_id') or '').strip()
        if not save_path or not nzo_id:
            continue

        dj = DownloadJob(
            library_id=library.id,
            source_file_path=job.source_path,
            release_name=matched_name,
            download_hash=nzo_id,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        logger.info(
            'SAB queue adoption: created download job %s for queued job %s from history item %r',
            dj.id,
            job.id,
            matched_name,
        )
        _import_file(db, dj, save_path, library, profile, qbt, sab)
        adopted += 1

    if adopted:
        logger.info('SAB queue adoption: imported %s completed artifact(s) for waiting queue jobs', adopted)
    return adopted


def _recover_sab_completed_for_existing_download_jobs(
    db: Session,
    qbt,
    sab,
    *,
    context: str = 'recovery',
) -> int:
    """Import completed SAB history entries into existing unimported DownloadJobs."""
    if not getattr(sab, 'enabled', False):
        return 0

    completed_items = download_client_service.get_sab_completed_history_items(sab)
    if not completed_items:
        return 0

    candidate_rows = (
        db.query(DownloadJob, Library, LibraryProfile)
        .join(Library, DownloadJob.library_id == Library.id)
        .join(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(
            DownloadJob.imported_file_path.is_(None),
            DownloadJob.status != DownloadJobStatus.complete.value,
            DownloadJob.status != DownloadJobStatus.importing.value,
            DownloadJob.status != DownloadJobStatus.pending.value,
        )
        .all()
    )
    if not candidate_rows:
        return 0

    imported = 0
    for dj, library, profile in candidate_rows:
        # Respect explicit qBit jobs.  Unknown client_type can still be adopted.
        if dj.client_type and dj.client_type != 'sabnzbd':
            continue

        keys = _build_completed_root_match_keys(dj)
        if not keys:
            continue

        matched_idx = None
        matched_item = None
        for idx, item in enumerate(completed_items):
            name = str(item.get('name') or '')
            name_key = _normalize_release_key(name)
            if not name_key:
                continue
            if any(k and (k in name_key or name_key in k) for k in keys):
                matched_idx = idx
                matched_item = item
                break
        if matched_item is None:
            continue

        matched_name = str(matched_item.get('name') or '')
        if not _release_title_matches_profile(matched_name, profile):
            logger.info(
                'SAB %s: download job %s matched completed history %r but failed profile check',
                context,
                dj.id,
                matched_name,
            )
            continue

        save_path = str(matched_item.get('save_path') or '').strip()
        nzo_id = str(matched_item.get('nzo_id') or '').strip()
        if not save_path or not nzo_id:
            continue

        dj.client_type = 'sabnzbd'
        dj.download_hash = nzo_id
        dj.release_name = matched_name
        dj.status = DownloadJobStatus.downloading.value
        dj.error_message = None
        db.commit()
        db.refresh(dj)

        logger.info(
            'SAB %s: importing existing download job %s from completed history %r',
            context,
            dj.id,
            matched_name,
        )
        _import_file(db, dj, save_path, library, profile, qbt, sab)
        imported += 1

        if matched_idx is not None:
            completed_items.pop(matched_idx)

    if imported:
        logger.info('SAB %s: imported %s completed history item(s) into existing download jobs', context, imported)
    return imported


# ─────────────────────────────────────────────────────────────────────────────
# Quality profile helpers
# ─────────────────────────────────────────────────────────────────────────────



def _normalize_release_title(title: str) -> str:
    """Normalize separators and case for quality token matching."""
    return re.sub(r'[._-]+', ' ', title or '').lower()


def _classify_release_quality(title: str) -> str | None:
    """Classify a release title into one of our quality profile buckets."""
    normalized = _normalize_release_title(title)

    has_remux = bool(re.search(r'\b(remux|bdremux)\b', normalized))
    has_web_dl = bool(
        re.search(
            r'\b(web\s?dl|webdl)\b|\b(amzn|nf|dsnp|atvp|hmax|hulu|itunes)\b',
            normalized,
        )
    )
    has_webrip = bool(re.search(r'\b(web\s?rip|webrip|webcap)\b', normalized))
    has_bluray = bool(re.search(r'\b(blu\s?ray|bdrip|bd\s?rip|brrip)\b', normalized))
    has_hdtv = bool(re.search(r'\b(hdtv|pdtv|dsr)\b', normalized))

    # Explicit conflict handling for mixed tags.
    if has_remux and (has_web_dl or has_webrip or has_bluray or has_hdtv):
        return DownloadQualityProfileEnum.remux.value
    if has_web_dl and has_webrip:
        return DownloadQualityProfileEnum.web_dl.value

    if has_remux:
        return DownloadQualityProfileEnum.remux.value
    if has_web_dl:
        return DownloadQualityProfileEnum.web_dl.value
    if has_webrip:
        return DownloadQualityProfileEnum.webrip.value
    if has_bluray:
        return DownloadQualityProfileEnum.bluray.value
    if has_hdtv:
        return DownloadQualityProfileEnum.hdtv.value
    return None


def _classify_release_quality_from_release(release: dict) -> str | None:
    """Classify quality using both the title and any structured quality fields."""
    title_quality = _classify_release_quality(str(release.get('title', '') or ''))
    if title_quality:
        return title_quality

    structured_candidates: list[str] = []
    for key in ('quality', 'qualityName', 'source'):
        value = release.get(key)
        if isinstance(value, str):
            structured_candidates.append(value)
        elif isinstance(value, dict):
            for nested_key in ('name', 'quality', 'source'):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    structured_candidates.append(nested)

    for candidate in structured_candidates:
        parsed = _classify_release_quality(candidate)
        if parsed:
            return parsed

    return None


def _quality_profile_value(profile: LibraryProfile) -> str:
    raw = getattr(profile, 'download_quality_profile', DownloadQualityProfileEnum.any)
    if isinstance(raw, DownloadQualityProfileEnum):
        return raw.value
    text = str(raw or DownloadQualityProfileEnum.any.value).strip().lower()
    aliases = {
        'webdl': DownloadQualityProfileEnum.web_dl.value,
        'web-dl': DownloadQualityProfileEnum.web_dl.value,
        'web dl': DownloadQualityProfileEnum.web_dl.value,
        'any': DownloadQualityProfileEnum.any.value,
        'remux': DownloadQualityProfileEnum.remux.value,
        'webrip': DownloadQualityProfileEnum.webrip.value,
        'bluray': DownloadQualityProfileEnum.bluray.value,
        'hdtv': DownloadQualityProfileEnum.hdtv.value,
    }
    normalized = aliases.get(text, text)
    if normalized not in {item.value for item in DownloadQualityProfileEnum}:
        logger.warning(
            'Unknown download_quality_profile=%r; defaulting to %r',
            raw,
            DownloadQualityProfileEnum.any.value,
        )
        return DownloadQualityProfileEnum.any.value
    return normalized


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or '').strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off', ''}:
        return False
    return bool(value)


def _release_matches_target_resolution(release: dict, target_resolution: int) -> bool:
    title_lower = str(release.get('title', '') or '').lower()
    target = int(target_resolution)

    if f'{target}p' in title_lower:
        return True
    if target == 2160 and '4k' in title_lower:
        return True
    if re.search(rf'\b\d{{3,4}}x{target}\b', title_lower):
        return True
    for key in ('resolution', 'quality'):
        value = release.get(key)
        if isinstance(value, str):
            value_lower = value.lower()
            if f'{target}p' in value_lower or (target == 2160 and '4k' in value_lower):
                return True
        elif isinstance(value, dict):
            nested_resolution = value.get('resolution')
            if isinstance(nested_resolution, int) and nested_resolution == target:
                return True
            nested_name = value.get('name')
            if isinstance(nested_name, str):
                nested_name_lower = nested_name.lower()
                if f'{target}p' in nested_name_lower or (target == 2160 and '4k' in nested_name_lower):
                    return True

    return False


def _profile_codec_value(profile: LibraryProfile) -> str:
    raw = getattr(profile, 'codec', 'hevc')
    value = getattr(raw, 'value', raw)
    return str(value or 'hevc').strip().lower()


def _profile_av1_fallback_codec_value(profile: LibraryProfile) -> str | None:
    raw = getattr(profile, 'av1_fallback_codec', None)
    value = getattr(raw, 'value', raw)
    normalized = str(value or '').strip().lower()
    return normalized or None


def _download_codec_value(profile: LibraryProfile) -> str:
    raw = getattr(profile, 'download_codec', None)
    value = getattr(raw, 'value', raw)
    normalized = str(value or '').strip().lower()
    return normalized or _profile_codec_value(profile)


def _download_fallback_codec_value(profile: LibraryProfile) -> str | None:
    raw = getattr(profile, 'download_fallback_codec', None)
    value = getattr(raw, 'value', raw)
    normalized = str(value or '').strip().lower()
    return normalized or None


def _allowed_download_codecs(profile: LibraryProfile) -> set[str]:
    target_codec = _download_codec_value(profile)
    allowed = {target_codec}
    if target_codec == 'av1':
        fallback_codec = _profile_av1_fallback_codec_value(profile)
        if fallback_codec and fallback_codec != 'av1':
            allowed.add(fallback_codec)
    download_fallback_codec = _download_fallback_codec_value(profile)
    if download_fallback_codec and download_fallback_codec != target_codec:
        allowed.add(download_fallback_codec)
    return allowed


def _detect_release_codecs(release: dict) -> set[str]:
    """
    Best-effort codec detection from release metadata/title.
    Returns an empty set when no codec token is detectable.
    """
    candidates: list[str] = [str(release.get('title', '') or '')]
    for key in ('codec', 'videoCodec'):
        value = release.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            for nested_key in ('name', 'codec', 'videoCodec'):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    candidates.append(nested)
    for key in ('quality', 'qualityName', 'source'):
        value = release.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            for nested_key in ('name', 'quality', 'source'):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    candidates.append(nested)

    detected: set[str] = set()
    for text in candidates:
        normalized = _normalize_release_title(text)
        if re.search(r'\b(av1|av01|svtav1)\b', normalized):
            detected.add('av1')
        if re.search(r'\b(hevc|x265)\b|\bh\s*265\b', normalized):
            detected.add('hevc')
        if re.search(r'\b(h264|x264|avc)\b|\bh\s*264\b', normalized):
            detected.add('h264')
    return detected


# ─────────────────────────────────────────────────────────────────────────────
# Search query construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_search_query(
    source_path: str,
    profile: LibraryProfile,
    *,
    include_resolution: bool | None = None,
) -> str:
    stem = Path(source_path).stem
    # Normalize dots/underscores to spaces
    clean = re.sub(r'[._]', ' ', stem)
    # Extract 4-digit year
    year_match = re.search(r'\b(19|20)\d{2}\b', clean)
    if year_match:
        year = year_match.group(0)
        # Strip any trailing punctuation/brackets left by the year pattern
        # e.g. "Movie Name (2024) [...]" → title up to start of "2024" contains "("
        title = re.sub(r'[\s()\[\]]+$', '', clean[:year_match.start()]).strip()
    else:
        year = ''
        title = clean.strip()
    if include_resolution is None:
        include_resolution = _infer_search_categories(source_path) != [2000]
    resolution = f"{profile.target_resolution}p" if include_resolution else ''
    # Quality term is intentionally excluded from the Prowlarr search so that
    # broad indexer results are returned.  Client-side filtering in
    # _select_best_release() then applies the exact quality class match.
    # Including the quality keyword in the search would silently exclude valid
    # releases when indexers use non-standard naming (e.g. "WEB.DL" instead of
    # "WEB-DL"), producing zero results even when good releases exist.
    return ' '.join(filter(None, [title, year, resolution]))


def _strip_bracketed_metadata(value: str) -> str:
    cleaned = re.sub(r'\{[^}]*\}', ' ', value or '')
    cleaned = re.sub(r'\[[^\]]*\]', ' ', cleaned)
    cleaned = re.sub(r'\([^)]*(?:imdb|tmdb|tvdb|tvmaze|trakt|rid)[^)]*\)', ' ', cleaned, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _extract_tagged_ids(source_path: str) -> dict[str, str]:
    normalized = str(source_path or '')
    patterns = {
        'imdbid': r'\bimdb[-_: ]?(tt\d{5,12})\b',
        'tmdbid': r'\btmdb[-_: ]?(\d+)\b',
        'tvdbid': r'\btvdb[-_: ]?(\d+)\b',
        'tvmazeid': r'\btvmaze[-_: ]?(\d+)\b',
        'rid': r'\brid[-_: ]?(\d+)\b',
    }
    extracted: dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            extracted[key] = match.group(1)
    return extracted


def _format_prowlarr_token(field: str, value: object) -> str:
    field_map = {
        'imdbid': 'ImdbId',
        'tmdbid': 'TmdbId',
        'tvdbid': 'TvdbId',
        'tvmazeid': 'TvMazeId',
        'rid': 'Rid',
        'season': 'Season',
        'episode': 'Episode',
        'year': 'Year',
    }
    return f'{{{field_map.get(field, field)}:{value}}}'


def _extract_tv_episode_details(source_path: str) -> dict[str, object]:
    stem = _strip_bracketed_metadata(Path(source_path or '').stem)
    clean_stem = re.sub(r'[._-]+', ' ', stem)
    clean_stem = re.sub(r'\s+', ' ', clean_stem).strip()
    season: int | None = None
    episode: int | None = None
    title = clean_stem

    match = re.search(r'\bS(?P<season>\d{1,2})E(?P<episode>\d{1,3})\b', clean_stem, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r'\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b', clean_stem, flags=re.IGNORECASE)

    if match is not None:
        season = int(match.group('season'))
        episode = int(match.group('episode'))
        title = re.sub(r'[\s()\[\]{}._-]+$', '', clean_stem[:match.start()]).strip()
    else:
        season_dir_match = re.search(r'[\\/]+Season\s*(\d+)\b', str(source_path or ''), flags=re.IGNORECASE)
        episode_match = re.search(r'\bEpisode\s*(\d{1,3})\b', clean_stem, flags=re.IGNORECASE)
        if season_dir_match and episode_match:
            season = int(season_dir_match.group(1))
            episode = int(episode_match.group(1))
            title = re.sub(r'[\s()\[\]{}._-]+$', '', clean_stem[:episode_match.start()]).strip()

    year_match = re.search(r'\b(19|20)\d{2}\b', _strip_bracketed_metadata(str(source_path or '')))
    year = int(year_match.group(0)) if year_match else None

    return {
        'title': title.strip(),
        'season': season,
        'episode': episode,
        'year': year,
        'ids': _extract_tagged_ids(source_path),
    }


def _build_prowlarr_query(
    source_path: str,
    profile: LibraryProfile,
    *,
    include_profile_hints: bool = False,
) -> dict[str, object]:
    categories = _infer_search_categories(source_path)
    base_query = _build_search_query(source_path, profile)
    query = _build_second_pass_search_query(source_path, profile) if include_profile_hints else base_query

    if categories == [5000]:
        details = _extract_tv_episode_details(source_path)
        season = details.get('season')
        episode = details.get('episode')
        title = str(details.get('title') or '').strip()
        if isinstance(season, int) and isinstance(episode, int) and title:
            ids = details.get('ids') if isinstance(details.get('ids'), dict) else {}
            year = details.get('year')
            query_terms = [title, f'{profile.target_resolution}p']
            if include_profile_hints:
                query_terms.extend(_quality_hint_tokens(profile))
                query_terms.extend(_codec_hint_tokens(profile))
                query_terms.extend(_hdr_hint_tokens(profile))

            token_parts: list[str] = []
            for key in ('imdbid', 'rid', 'tvdbid', 'tmdbid', 'tvmazeid'):
                value = ids.get(key) if isinstance(ids, dict) else None
                if value:
                    token_parts.append(_format_prowlarr_token(key, value))
            token_parts.append(_format_prowlarr_token('season', season))
            token_parts.append(_format_prowlarr_token('episode', episode))
            if isinstance(year, int):
                token_parts.append(_format_prowlarr_token('year', year))

            return {
                'query': f'{" ".join(dict.fromkeys(filter(None, query_terms)))} {"".join(token_parts)}'.strip(),
                'categories': categories,
                'search_type': 'tvsearch',
            }

    if categories == [2000]:
        ids = _extract_tagged_ids(source_path)
        year = _extract_source_title_and_year(source_path)[1]
        token_parts = []
        query_prefix = query

        imdb_id = ids.get('imdbid')
        tmdb_id = ids.get('tmdbid')
        if imdb_id:
            query_prefix = ''
            token_parts.append(_format_prowlarr_token('imdbid', imdb_id))
        elif tmdb_id:
            query_prefix = ''
            token_parts.append(_format_prowlarr_token('tmdbid', tmdb_id))

        if not token_parts:
            if tmdb_id:
                token_parts.append(_format_prowlarr_token('tmdbid', tmdb_id))
            if isinstance(year, int):
                token_parts.append(_format_prowlarr_token('year', year))
        elif not imdb_id and isinstance(year, int):
            token_parts.append(_format_prowlarr_token('year', year))

        if token_parts:
            return {
                'query': f'{query_prefix} {"".join(token_parts)}'.strip(),
                'categories': categories,
                'search_type': 'movie',
            }

    return {'query': query, 'categories': categories, 'search_type': None}


def _build_generic_tv_search_query(
    source_path: str,
    profile: LibraryProfile,
    *,
    include_profile_hints: bool = False,
) -> dict[str, object]:
    details = _extract_tv_episode_details(source_path)
    title = str(details.get('title') or '').strip()
    season = details.get('season')
    episode = details.get('episode')
    year = details.get('year')
    if not title or not isinstance(season, int) or not isinstance(episode, int):
        base_query = _build_second_pass_search_query(source_path, profile) if include_profile_hints else _build_search_query(source_path, profile)
        return {'query': base_query, 'categories': [5000], 'search_type': None}

    query_terms = [title]
    if isinstance(year, int):
        query_terms.append(str(year))
    query_terms.append(f'S{season:02d}E{episode:02d}')
    query_terms.append(f'{profile.target_resolution}p')

    if include_profile_hints:
        query_terms.extend(_quality_hint_tokens(profile))
        query_terms.extend(_codec_hint_tokens(profile))
        query_terms.extend(_hdr_hint_tokens(profile))

    return {
        'query': ' '.join(filter(None, query_terms)),
        'categories': [5000],
        'search_type': None,
    }


def _build_generic_movie_search_query(
    source_path: str,
    profile: LibraryProfile,
    *,
    include_profile_hints: bool = False,
) -> dict[str, object]:
    title, _year = _extract_source_title_and_year(source_path)
    title = str(title or '').strip()
    if not title:
        base_query = _build_second_pass_search_query(source_path, profile) if include_profile_hints else _build_search_query(source_path, profile)
        return {'query': base_query, 'categories': [2000], 'search_type': None}

    query_terms = [title, f'{profile.target_resolution}p']
    if include_profile_hints:
        query_terms.extend(_quality_hint_tokens(profile))
        query_terms.extend(_codec_hint_tokens(profile))
        query_terms.extend(_hdr_hint_tokens(profile))

    return {
        'query': ' '.join(filter(None, query_terms)),
        'categories': [2000],
        'search_type': None,
    }


def _quality_hint_tokens(profile: LibraryProfile) -> list[str]:
    quality_val = _quality_profile_value(profile)
    if quality_val == DownloadQualityProfileEnum.web_dl.value:
        return ['WEB-DL']
    if quality_val == DownloadQualityProfileEnum.webrip.value:
        return ['WEBRip']
    if quality_val == DownloadQualityProfileEnum.bluray.value:
        return ['BluRay']
    if quality_val == DownloadQualityProfileEnum.remux.value:
        return ['REMUX']
    if quality_val == DownloadQualityProfileEnum.hdtv.value:
        return ['HDTV']
    return []


def _codec_hint_tokens(profile: LibraryProfile) -> list[str]:
    codec = _download_codec_value(profile)
    if codec == 'hevc':
        return ['HEVC']
    if codec == 'av1':
        return ['AV1']
    if codec == 'h264':
        return ['H264']
    return []


def _hdr_hint_tokens(profile: LibraryProfile) -> list[str]:
    tone_map_hdr = _coerce_bool(getattr(profile, 'tone_map_hdr', False))
    if tone_map_hdr:
        return ['SDR']
    return []


def _build_second_pass_search_query(source_path: str, profile: LibraryProfile) -> str:
    base = _build_search_query(source_path, profile)
    tokens = [base]
    tokens.extend(_quality_hint_tokens(profile))
    tokens.extend(_codec_hint_tokens(profile))
    tokens.extend(_hdr_hint_tokens(profile))
    return ' '.join(filter(None, tokens))


def _infer_search_categories(source_path: str) -> list[int] | None:
    normalized_path = re.sub(r'[._-]+', ' ', str(source_path or '')).lower()
    tv_patterns = (
        r'\bs\d{1,2}e\d{1,3}\b',
        r'\b\d{1,2}x\d{1,3}\b',
        r'\bseason\s*\d+\b',
        r'\bepisode\s*\d+\b',
        r'/season\s*\d+\b',
        r'/series\b',
    )
    if any(re.search(pattern, normalized_path) for pattern in tv_patterns):
        return [5000]

    stem = Path(source_path or '').stem
    clean_stem = re.sub(r'[._-]+', ' ', stem)
    if re.search(r'\b(19|20)\d{2}\b', clean_stem):
        return [2000]

    return None


def _search_passes_for_job(dj: DownloadJob, profile: LibraryProfile) -> list[dict[str, object]]:
    base_search = _build_prowlarr_query(dj.source_file_path, profile, include_profile_hints=False)
    hinted_search = _build_prowlarr_query(dj.source_file_path, profile, include_profile_hints=True)

    search_passes: list[dict[str, object]] = [
        {'name': 'broad', **base_search}
    ]
    if hinted_search.get('query') != base_search.get('query'):
        search_passes.append({'name': 'profile_hint', **hinted_search})

    if base_search.get('categories') == [5000] and base_search.get('search_type') == 'tvsearch':
        generic_tv_search = _build_generic_tv_search_query(dj.source_file_path, profile, include_profile_hints=False)
        if generic_tv_search.get('query') != base_search.get('query'):
            search_passes.append({'name': 'title_fallback', **generic_tv_search})

        generic_tv_hinted_search = _build_generic_tv_search_query(dj.source_file_path, profile, include_profile_hints=True)
        if generic_tv_hinted_search.get('query') not in {base_search.get('query'), hinted_search.get('query'), generic_tv_search.get('query')}:
            search_passes.append({'name': 'title_fallback_profile_hint', **generic_tv_hinted_search})
    elif base_search.get('categories') == [2000]:
        generic_movie_search = _build_generic_movie_search_query(dj.source_file_path, profile, include_profile_hints=False)
        if generic_movie_search.get('query') not in {base_search.get('query'), hinted_search.get('query')}:
            search_passes.append({'name': 'title_fallback', **generic_movie_search})

        generic_movie_hinted_search = _build_generic_movie_search_query(dj.source_file_path, profile, include_profile_hints=True)
        if generic_movie_hinted_search.get('query') not in {
            base_search.get('query'),
            hinted_search.get('query'),
            generic_movie_search.get('query'),
        }:
            search_passes.append({'name': 'title_fallback_profile_hint', **generic_movie_hinted_search})
    return search_passes


def _extract_source_title_and_year(source_path: str) -> tuple[str, int | None]:
    stem = Path(source_path or '').stem
    clean = re.sub(r'[._]', ' ', stem)
    year_match = re.search(r'\b(19|20)\d{2}\b', clean)
    year: int | None = int(year_match.group(0)) if year_match else None
    if year_match:
        title = re.sub(r'[\s()\[\]{}._-]+$', '', clean[:year_match.start()]).strip()
    else:
        title = clean.strip()
    return title, year


def _title_tokens_for_matching(value: str) -> list[str]:
    normalized = _normalize_release_title(value)
    raw_tokens = re.findall(r'[a-z0-9]+', normalized)
    stopwords = {'the', 'a', 'an', 'and', 'movie'}
    tokens = [token for token in raw_tokens if len(token) > 2 and token not in stopwords]

    # Preserve sequel markers so "Part One" / "Part Two" style titles can be
    # distinguished even though short numerals are usually dropped.
    sequel_markers = re.findall(r'\b(?:part|pt)\s*([0-9ivx]+)\b', normalized)
    for marker in sequel_markers:
        token = f'part{marker}'
        if token not in tokens:
            tokens.append(token)
    return tokens


def _is_probable_tv_episode_title(title: str) -> bool:
    normalized = _normalize_release_title(title)
    patterns = (
        r'\bs\d{1,2}e\d{1,3}\b',
        r'\bseason\s*\d+\b',
        r'\bepisode\s*\d+\b',
        r'\bcomplete\s+series\b',
        r'\bseries\s+pack\b',
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _extract_season_episode(value: str) -> tuple[int | None, int | None]:
    normalized = _normalize_release_title(value)
    match = re.search(r'\bs(?P<season>\d{1,2})e(?P<episode>\d{1,3})\b', normalized, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r'\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b', normalized, flags=re.IGNORECASE)
    if match is None:
        return None, None
    return int(match.group('season')), int(match.group('episode'))


def _release_has_multi_episode_marker(release_title: str) -> bool:
    text = str(release_title or '').lower()
    # Keep hyphens for range detection, but normalize common filename separators.
    text = re.sub(r'[._]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    range_patterns = (
        r'\bs\d{1,2}e(?P<start>\d{1,3})\s*(?:-|to|thru|through)\s*e?(?P<end>\d{1,3})\b',
        r'\b\d{1,2}x(?P<start>\d{1,3})\s*(?:-|to|thru|through)\s*(?P<end>\d{1,3})\b',
        r'\b(?:episodes?|eps?)\s*(?P<start>\d{1,3})\s*(?:-|to|thru|through)\s*(?P<end>\d{1,3})\b',
    )
    for pattern in range_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and int(match.group('start')) != int(match.group('end')):
            return True

    # Compact multi-episode naming such as S01E01E02 or repeated S01E01 S01E02.
    if re.search(r'\bs\d{1,2}e\d{1,3}(?:\s*e\d{1,3})+\b', text, flags=re.IGNORECASE):
        return True
    episode_codes = re.findall(r'\bs\d{1,2}e\d{1,3}\b|\b\d{1,2}x\d{1,3}\b', text, flags=re.IGNORECASE)
    return len(set(episode_codes)) > 1


def _release_matches_source_title(release_title: str, source_path: str) -> bool:
    source_season, source_episode = _extract_season_episode(source_path)
    source_title, source_year = _extract_source_title_and_year(source_path)
    if source_season is not None and source_episode is not None:
        tv_details = _extract_tv_episode_details(source_path)
        tv_title = str(tv_details.get('title') or '').strip()
        if tv_title:
            source_title = tv_title
        tv_year = tv_details.get('year')
        if isinstance(tv_year, int):
            source_year = tv_year
    source_tokens = _title_tokens_for_matching(source_title)
    if not source_tokens:
        return True

    normalized_release = _normalize_release_title(release_title)
    release_tokens = _title_tokens_for_matching(release_title)
    if not release_tokens:
        return False

    release_season, release_episode = _extract_season_episode(release_title)
    if source_season is not None and source_episode is not None:
        if _release_has_multi_episode_marker(release_title):
            return False
        if release_season != source_season or release_episode != source_episode:
            return False
    elif _is_probable_tv_episode_title(release_title):
        return False

    if source_year is not None:
        release_years = {int(match) for match in re.findall(r'\b(?:19|20)\d{2}\b', release_title)}
        if release_years and source_year not in release_years:
            return False

    overlap = len(set(source_tokens) & set(release_tokens))
    if len(source_tokens) >= 2:
        # Require stronger overlap for longer multi-word titles to reduce
        # accidental grabs from similarly named releases.
        token_count = len(set(source_tokens))
        required_overlap = min(token_count, max(2, math.ceil(token_count * 0.7)))
        return overlap >= required_overlap

    # Single-word titles are ambiguous ("Wicked", "It", etc.). Require the
    # token to appear near the start of the release title to avoid partial
    # matches from unrelated episode names.
    token = source_tokens[0]
    match = re.search(rf'\b{re.escape(token)}\b', normalized_release)
    if not match:
        return False
    return match.start() <= 12


def _is_hdr_release(title_lower: str) -> bool:
    """Return True if the release title indicates HDR content."""
    # 'hdr' catches hdr, hdr10, hdr10+; 'dolby vision' catches full name
    if any(tag in title_lower for tag in ('hdr', 'dolby vision')):
        return True
    # Word-boundary match for standalone 'dv' or 'hlg' to avoid false positives
    return bool(re.search(r'\b(dv|hlg)\b', title_lower))


def _source_allows_variant(source_path: str, required_tokens: tuple[str, ...]) -> bool:
    source_title, _ = _extract_source_title_and_year(source_path or '')
    source_tokens = set(_title_tokens_for_matching(source_title))

    def _has_token(token: str) -> bool:
        if token in source_tokens:
            return True
        if token.endswith('s') and token[:-1] in source_tokens:
            return True
        if f'{token}s' in source_tokens:
            return True
        return False

    return all(_has_token(token) for token in required_tokens)


def _is_unwanted_variant_release(title: str, source_path: str = '') -> bool:
    """Return True when a release title matches a blocked edition/variant token."""
    if not title:
        return False
    for pattern in _UNWANTED_RELEASE_VARIANT_PATTERNS:
        if not pattern.search(title):
            continue
        required_tokens = _UNWANTED_RELEASE_VARIANT_REQUIRED_TOKENS.get(pattern, ())
        if required_tokens and source_path and _source_allows_variant(source_path, required_tokens):
            return False
        return True
    return False


def _rank_candidates(releases: list[dict]) -> list[dict]:
    """
    Sort releases by Prowlarr indexer priority (lower wins), then seeders
    descending, then size ascending.
    """
    def _safe_int(value: object, default: int = 0) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return sorted(
        releases,
        key=lambda r: (
            _safe_int(r.get('_indexer_priority', 10_000_000), 10_000_000),
            -_safe_int(r.get('seeders', 0), 0),
            _safe_int(r.get('size', 0), 0),
        ),
    )


def _select_best_release(
    releases: list[dict],
    profile: LibraryProfile,
    qbt_enabled: bool,
    sab_enabled: bool,
    indexer_by_id: dict[int, dict] | None = None,
    source_path: str = '',
) -> dict | None:
    """
    Select the best release at the target resolution, respecting the
    configured quality profile filter, tone-map/HDR policy, and enabled client.

    If tone_map_hdr is enabled, HDR releases are rejected.
    If a specific quality profile is set only releases whose title contains
    a matching keyword are accepted.
    Torrent releases require qBittorrent to be enabled; usenet releases
    require SABnzbd to be enabled.
    """
    quality_val = _quality_profile_value(profile)
    indexer_by_id = indexer_by_id or {}
    candidates: list[dict] = []
    tone_map_hdr = _coerce_bool(getattr(profile, 'tone_map_hdr', False))
    filtered_by_resolution = 0
    filtered_by_tonemap_hdr = 0
    filtered_by_quality = 0
    filtered_by_client = 0
    filtered_by_variant = 0
    filtered_by_codec = 0
    allowed_codecs = _allowed_download_codecs(profile)

    for r in releases:
        title = r.get('title', '')
        title_lower = title.lower()
        quality_class = _classify_release_quality_from_release(r)

        # Resolution filter
        if not _release_matches_target_resolution(r, profile.target_resolution):
            filtered_by_resolution += 1
            continue

        is_hdr = _is_hdr_release(title_lower)
        if tone_map_hdr and is_hdr:
            filtered_by_tonemap_hdr += 1
            continue

        if _is_unwanted_variant_release(title, source_path):
            filtered_by_variant += 1
            continue

        # Codec filter: if codec is detectable and doesn't match the library
        # target codec set, skip this release. AV1 profiles may explicitly
        # allow their configured fallback codec.
        detected_codecs = _detect_release_codecs(r)
        if detected_codecs and not (detected_codecs & allowed_codecs):
            filtered_by_codec += 1
            continue

        # Quality source filter
        if quality_val != DownloadQualityProfileEnum.any.value and quality_class != quality_val:
            filtered_by_quality += 1
            continue

        # Protocol / client availability filter
        protocol = r.get('protocol', '').lower()
        if protocol == 'torrent' and not qbt_enabled:
            filtered_by_client += 1
            continue
        if protocol == 'usenet' and not sab_enabled:
            filtered_by_client += 1
            continue

        indexer_id_raw = r.get('indexerId')
        try:
            indexer_id = int(indexer_id_raw) if indexer_id_raw is not None else None
        except (TypeError, ValueError):
            indexer_id = None
        indexer_meta = indexer_by_id.get(indexer_id, {}) if indexer_id is not None else {}
        indexer_name = str(r.get('indexer') or indexer_meta.get('name') or '').strip()
        priority_raw = indexer_meta.get('priority')
        try:
            indexer_priority = int(priority_raw)
        except (TypeError, ValueError):
            indexer_priority = 10_000_000

        candidates.append(
            {
                **r,
                '_quality_class': quality_class,
                '_indexer_id': indexer_id,
                '_indexer_name': indexer_name,
                '_indexer_priority': indexer_priority,
            }
        )

    if candidates:
        # Tie-break: prefer exact profile class matches before seed/size ranking.
        if quality_val != DownloadQualityProfileEnum.any.value:
            exact_matches = [r for r in candidates if r.get('_quality_class') == quality_val]
            if exact_matches:
                return _rank_candidates(exact_matches)[0]
        return _rank_candidates(candidates)[0]

    logger.info(
        'Download filtering produced no candidates: total=%d filtered_resolution=%d '
        'filtered_tone_map_hdr=%d filtered_quality=%d '
        'filtered_variant=%d filtered_codec=%d '
        'filtered_client=%d profile={target_resolution=%s quality=%s tone_map_hdr=%s} '
        'clients={qbt=%s sab=%s}',
        len(releases),
        filtered_by_resolution,
        filtered_by_tonemap_hdr,
        filtered_by_quality,
        filtered_by_variant,
        filtered_by_codec,
        filtered_by_client,
        getattr(profile, 'target_resolution', None),
        quality_val,
        tone_map_hdr,
        qbt_enabled,
        sab_enabled,
    )
    return None  # no matching release found; caller falls back to encoding


def _client_type_for_protocol(protocol: str) -> str:
    """Map Prowlarr release protocol to our client_type string."""
    if protocol.lower() == 'torrent':
        return 'qbittorrent'
    return 'sabnzbd'


def _extract_hash_from_guid(guid: str) -> str:
    """Extract a 40-character BitTorrent info-hash embedded in a GUID URL.

    Many indexers encode the torrent hash directly in the GUID path, e.g.:
      https://www.torrentdownload.info/<HASH>/release-name
    When Prowlarr's grab response omits 'downloadId' and 'hash' fields we can
    still recover the hash from the GUID so tracking doesn't fall back to the
    slow name-matching heuristic.
    """
    if not guid:
        return ''
    # SHA-1 info-hash: exactly 40 hex characters, not part of a longer hex run
    match = re.search(r'(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])', guid)
    return match.group(1) if match else ''


def _normalize_qbt_info_hash(value: object) -> str:
    candidate = str(value or '').strip().lower()
    return candidate if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', candidate) else ''


def _extract_qbt_info_hash(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    normalized = _normalize_qbt_info_hash(text)
    if normalized:
        return normalized
    btih = re.search(r'btih:([A-Za-z0-9]{32,64})', text, re.IGNORECASE)
    if btih:
        normalized = _normalize_qbt_info_hash(btih.group(1))
        if normalized:
            return normalized
    match = re.search(r'(?<![0-9a-fA-F])([0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9a-fA-F])', text)
    return match.group(1).lower() if match else ''


def _extract_hash_from_release(release: dict) -> str:
    """Best-effort info-hash extraction from a Prowlarr release payload."""
    if not isinstance(release, dict):
        return ''
    candidates = [
        release.get('infoHash'),
        release.get('downloadId'),
        release.get('hash'),
        release.get('magnetUrl'),
        release.get('downloadUrl'),
        release.get('guid'),
        release.get('infoUrl'),
        release.get('comments'),
    ]
    for raw in candidates:
        extracted = _extract_qbt_info_hash(raw)
        if extracted:
            return extracted
    return ''


def _find_existing_qbt_torrent_for_release(release: dict, torrents: list[dict]) -> dict | None:
    """Return an already-present qBit torrent for a release when safely identifiable.

    Exact infohash matches are preferred because they are stable across retries
    and avoid duplicate grabs of the same underlying torrent. As a fallback,
    reuse an exact-name match only when that torrent is already tagged by
    Optimizarr, which keeps the match conservative and avoids attaching to an
    unrelated manually-added torrent with the same title.
    """
    if not torrents:
        return None

    release_hash = _extract_hash_from_release(release)
    if release_hash:
        release_hash = release_hash.lower()
        for torrent in torrents:
            torrent_hash = str(torrent.get('hash') or '').strip().lower()
            if torrent_hash and torrent_hash == release_hash:
                return torrent

    release_title = str(release.get('title') or '').strip()
    if not release_title:
        return None

    exact_tagged_matches = [
        torrent for torrent in torrents
        if str(torrent.get('name') or '').strip() == release_title and _is_optimizarr_tagged(torrent)
    ]
    if not exact_tagged_matches:
        return None
    return max(exact_tagged_matches, key=lambda torrent: int(torrent.get('added_on') or 0))


def _extract_year_from_path(path: str | None) -> int | None:
    if not path:
        return None
    stem = Path(path).stem
    spaced = re.sub(r'[._]', ' ', stem)
    paren_match = re.search(r'\(((?:19|20)\d{2})\)', spaced)
    if paren_match:
        return int(paren_match.group(1))
    year_match = re.search(r'\b((?:19|20)\d{2})\b', spaced)
    if year_match:
        return int(year_match.group(1))
    return None


def _pending_download_sort_option(db: Session) -> str:
    settings = db.query(Settings).first()
    raw_sort_option = getattr(settings, 'queue_sort', QueueSortEnum.default) if settings is not None else QueueSortEnum.default
    return str(getattr(raw_sort_option, 'value', raw_sort_option))


def _select_next_pending_download_job(db: Session) -> DownloadJob | None:
    sort_option = _pending_download_sort_option(db)
    base_query = db.query(DownloadJob).filter(DownloadJob.status == DownloadJobStatus.pending.value)

    if sort_option == QueueSortEnum.newest.value:
        pending_jobs = base_query.order_by(DownloadJob.created_at.desc(), DownloadJob.id.desc()).all()
    elif sort_option in {QueueSortEnum.default.value, QueueSortEnum.oldest.value}:
        pending_jobs = base_query.order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc()).all()
    else:
        pending_jobs = base_query.all()
    if not pending_jobs:
        return None

    if sort_option not in {QueueSortEnum.default.value, QueueSortEnum.oldest.value, QueueSortEnum.newest.value}:
        def sort_key(job: DownloadJob) -> tuple[int, int]:
            if sort_option == QueueSortEnum.year_newest.value:
                year = _extract_year_from_path(job.source_file_path)
                return (-(year if year is not None else 0), -job.id)
            if sort_option == QueueSortEnum.year_oldest.value:
                year = _extract_year_from_path(job.source_file_path)
                return ((year if year is not None else 9999), job.id)
            created_ts = job.created_at.timestamp() if job.created_at else 0
            return (int(created_ts), job.id)

        pending_jobs.sort(key=sort_key)

    for job in pending_jobs:
        blocker = _download_job_identity_blocker(db, job)
        if blocker is not None:
            _remove_duplicate_unstarted_download_job(db, job, blocker)
            continue
        return job
    return None


def _extract_title_tokens(source_path: str) -> list[str]:
    stem = Path(source_path or '').stem.lower()
    stem = re.sub(r'[\._-]+', ' ', stem)
    stem = re.sub(r'\(?(19|20)\d{2}\)?', ' ', stem)
    raw_tokens = re.findall(r'[a-z0-9]+', stem)
    stopwords = {'the', 'a', 'an', 'and', 'part', 'pt', 'movie'}
    tokens = [t for t in raw_tokens if len(t) > 2 and t not in stopwords]
    return tokens[:8]


def _release_selection_key_from_release(release: dict) -> str:
    guid = str(release.get('guid') or '').strip()
    if guid:
        return f'guid:{guid}'
    title = _normalize_release_key(str(release.get('title') or ''))
    if not title:
        return ''
    idx_raw = release.get('indexerId')
    try:
        indexer_id = int(idx_raw) if idx_raw is not None else 0
    except (TypeError, ValueError):
        indexer_id = 0
    protocol = str(release.get('protocol') or '').strip().lower()
    return f'title:{title}:idx:{indexer_id}:proto:{protocol}'


def _protocol_exclusion_key(protocol: str) -> str:
    return f'protocol:{str(protocol or "").strip().lower()}'


def _load_excluded_protocols(dj: DownloadJob) -> set[str]:
    keys = _load_failed_release_keys(dj)
    excluded: set[str] = set()
    for key in keys:
        if key.startswith('protocol:'):
            excluded.add(key.split(':', 1)[1].strip().lower())
    return {item for item in excluded if item}


def _release_selection_key_from_job(dj: DownloadJob) -> str:
    if dj.selected_release_key:
        return str(dj.selected_release_key).strip()
    title = _normalize_release_key(dj.release_name)
    if not title:
        return ''
    indexer_id = int(dj.indexer_id or 0)
    protocol = str(dj.client_type or '').strip().lower()
    return f'title:{title}:idx:{indexer_id}:proto:{protocol}'


def _load_failed_release_keys(dj: DownloadJob) -> set[str]:
    raw = str(dj.failed_release_keys or '').strip()
    if not raw:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item).strip() for item in payload if str(item).strip()}


def _client_tracking_grace_active(dj: DownloadJob) -> bool:
    reference = dj.download_started_at or dj.created_at
    if reference is None:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - reference
    return elapsed < timedelta(seconds=_CLIENT_TRACKING_GRACE_SECONDS)


def _store_failed_release_keys(dj: DownloadJob, keys: set[str]) -> None:
    if not keys:
        dj.failed_release_keys = None
        return
    dj.failed_release_keys = json.dumps(sorted(keys))


def _retry_failed_download(
    db: Session,
    dj: DownloadJob,
    library: Library,
    profile: LibraryProfile,
    *,
    reason: str,
    failed_release_key: str = '',
) -> bool:
    max_retries = int(getattr(dj, 'max_retries', _DEFAULT_DOWNLOAD_MAX_RETRIES) or _DEFAULT_DOWNLOAD_MAX_RETRIES)
    retry_next = int(getattr(dj, 'retry_count', 0) or 0) + 1

    if failed_release_key:
        keys = _load_failed_release_keys(dj)
        keys.add(failed_release_key)
        _store_failed_release_keys(dj, keys)

    if retry_next <= max_retries:
        dj.retry_count = retry_next
        dj.max_retries = max_retries
        dj.status = DownloadJobStatus.searching.value
        dj.error_message = f'{reason}; retrying {retry_next}/{max_retries}'
        dj.release_name = None
        dj.indexer_id = None
        dj.indexer_name = None
        dj.selected_release_key = None
        dj.download_hash = None
        dj.client_type = None
        dj.progress_percent = 0
        dj.eta_seconds = None
        dj.download_speed_bps = None
        dj.download_started_at = None
        dj.completed_at = None
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        _wake_event.set()
        logger.warning('Download job %s: %s (auto-retry %s/%s)', dj.id, reason, retry_next, max_retries)
        return True

    if str(dj.client_type or '').lower() == 'sabnzbd':
        qbt = download_client_service.get_or_create_qbt_settings(db)
        excluded_protocols = _load_excluded_protocols(dj)
        if qbt.enabled and 'usenet' not in excluded_protocols:
            keys = _load_failed_release_keys(dj)
            keys.add(_protocol_exclusion_key('usenet'))
            _store_failed_release_keys(dj, keys)
            dj.retry_count = 0
            dj.max_retries = max_retries
            dj.status = DownloadJobStatus.searching.value
            dj.error_message = f'{reason}; usenet retries exhausted, switching to torrent search'
            dj.release_name = None
            dj.indexer_id = None
            dj.indexer_name = None
            dj.selected_release_key = None
            dj.download_hash = None
            dj.client_type = None
            dj.progress_percent = 0
            dj.eta_seconds = None
            dj.download_speed_bps = None
            dj.download_started_at = None
            dj.completed_at = None
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
            _wake_event.set()
            logger.warning(
                'Download job %s: %s; usenet retries exhausted (%s/%s), switching to torrents',
                dj.id,
                reason,
                retry_next - 1,
                max_retries,
            )
            return True

    logger.warning('Download job %s: %s; retries exhausted (%s/%s)', dj.id, reason, retry_next - 1, max_retries)
    _mark_failed(db, dj, f'{reason}; retries exhausted after {max_retries} attempts')
    _fallback_to_encode(db, dj, library, profile)
    return False


def _is_optimizarr_tagged(torrent: dict) -> bool:
    tags_value = str(torrent.get('tags') or '')
    tags = {part.strip().lower() for part in tags_value.split(',') if part.strip()}
    return 'optimizarr' in tags


def _recover_qbt_hash_for_job(dj: DownloadJob, torrents: list[dict]) -> str:
    """Recover a qBit hash for jobs where Prowlarr grab did not return one."""
    if not torrents:
        logger.info('Download job %s hash-recovery: no qBit torrents available', dj.id)
        return ''
    match = _find_qbt_torrent_for_release(dj, torrents)
    if match and match.get('hash'):
        recovered = str(match.get('hash')).lower()
        logger.info('Download job %s hash-recovery: using name-based match hash=%s', dj.id, recovered)
        return recovered

    tokens = _extract_title_tokens(dj.source_file_path)
    if not tokens:
        logger.info('Download job %s hash-recovery: no source tokens extracted from %r', dj.id, dj.source_file_path)
        return ''
    job_ts = dj.download_started_at or dj.created_at
    job_epoch = int(job_ts.replace(tzinfo=timezone.utc).timestamp()) if job_ts else 0

    scored: list[tuple[int, int, int, dict]] = []
    for torrent in torrents:
        name = str(torrent.get('name') or '').lower()
        if not name:
            continue
        overlap = sum(1 for token in tokens if token in name)
        if overlap == 0:
            continue
        added_on = int(torrent.get('added_on') or 0)
        recency_bonus = 1 if added_on >= (job_epoch - 600) else 0
        tagged_bonus = 1 if _is_optimizarr_tagged(torrent) else 0
        scored.append((overlap + recency_bonus, tagged_bonus, added_on, torrent))

    if not scored:
        logger.info(
            'Download job %s hash-recovery: token overlap found no candidates; tokens=%s torrent_count=%d',
            dj.id,
            tokens,
            len(torrents),
        )
        return ''
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    winner = scored[0][3]
    recovered = str(winner.get('hash') or '').lower()
    top = scored[:3]
    top_preview = [
        {
            'score': item[0],
            'tagged': bool(item[1]),
            'added_on': item[2],
            'hash': str(item[3].get('hash', '')).lower(),
            'name': item[3].get('name'),
        }
        for item in top
    ]
    logger.info(
        'Download job %s hash-recovery: token match selected hash=%s using tokens=%s top_candidates=%s',
        dj.id,
        recovered,
        tokens,
        top_preview,
    )
    return recovered


# ─────────────────────────────────────────────────────────────────────────────
# Processing: searching → downloading
# ─────────────────────────────────────────────────────────────────────────────

def _process_searching_jobs(db: Session) -> None:
    if _download_queue_stopped:
        return
    if _scan_recovery_event.is_set():
        return
    # Keep download searching aligned with the main queue state so manual scans
    # can stage items without immediately starting Prowlarr searches.
    from app.workers.queue import is_queue_paused
    if is_queue_paused():
        return

    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)

    if not prowlarr.enabled or (not qbt.enabled and not sab.enabled):
        return

    # Always process an existing searching job first so transient errors
    # can be retried and startup resumes in-flight search work.
    searching_job = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.searching.value)
        .order_by(DownloadJob.created_at.asc())
        .first()
    )
    if searching_job is not None:
        _do_search(db, searching_job, prowlarr, qbt, sab)
        return

    global _startup_grace_until
    if _startup_grace_until is not None:
        if datetime.now(timezone.utc) < _startup_grace_until:
            remaining = int((_startup_grace_until - datetime.now(timezone.utc)).total_seconds())
            logger.info(
                'Download monitor: startup grace active; deferring searches for ~%ss (until %s UTC)',
                max(0, remaining),
                _startup_grace_until.strftime('%H:%M:%S'),
            )
            return
        _startup_grace_until = None  # expired — clear and proceed normally

    # Pick the next pending job using the configured queue sort policy,
    # promote it to searching, then run the search.
    dj = _select_next_pending_download_job(db)
    if dj:
        dj.status = DownloadJobStatus.searching.value
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        _do_search(db, dj, prowlarr, qbt, sab)


def _do_search(db: Session, dj: DownloadJob, prowlarr, qbt, sab) -> None:
    library = db.query(Library).filter(Library.id == dj.library_id).first()
    if library is None or library.profile is None:
        _mark_failed(db, dj, 'Library or profile not found')
        return

    profile = library.profile
    search_passes = _search_passes_for_job(dj, profile)
    dj.search_query = str(search_passes[0]['query'])
    dj.indexer_id = None
    dj.indexer_name = None
    db.commit()
    indexers = prowlarr_service.get_indexers(prowlarr)
    indexer_by_id: dict[int, dict] = {}
    for idx in indexers:
        if not isinstance(idx, dict):
            continue
        idx_id_raw = idx.get('id')
        try:
            idx_id = int(idx_id_raw)
        except (TypeError, ValueError):
            continue
        indexer_by_id[idx_id] = idx
    failed_release_keys = _load_failed_release_keys(dj)
    excluded_protocols = _load_excluded_protocols(dj)
    best = None
    matched_query = dj.search_query
    for pass_index, search_pass in enumerate(search_passes, start=1):
        query = str(search_pass['query'])
        categories = search_pass.get('categories')
        search_type = search_pass.get('search_type')
        dj.search_query = query
        db.commit()

        logger.info(
            'Download job %s searching Prowlarr for %r (pass=%d/%d strategy=%s categories=%s)',
            dj.id,
            query,
            pass_index,
            len(search_passes),
            search_pass.get('name'),
            categories,
        )
        releases = prowlarr_service.search(
            prowlarr,
            query,
            categories=categories if isinstance(categories, list) else None,
            search_type=str(search_type) if search_type else None,
        )

        # None means a connection/HTTP error — leave the job in 'searching' so the
        # monitor retries on the next poll cycle instead of immediately failing.
        if releases is None:
            logger.warning('Download job %s: Prowlarr search failed on pass %d (connection error); will retry', dj.id, pass_index)
            return

        protocol_counts = Counter(
            str(release.get('protocol', '') or '').strip().lower() or 'unknown'
            for release in releases
        )
        logger.info(
            'Download job %s: Prowlarr returned %d release(s) by protocol on pass %d: %s',
            dj.id,
            len(releases),
            pass_index,
            dict(protocol_counts),
        )
        if failed_release_keys:
            pre_filter_count = len(releases)
            releases = [
                release
                for release in releases
                if _release_selection_key_from_release(release) not in failed_release_keys
            ]
            logger.info(
                'Download job %s: excluded %d previously failed release(s) on pass %d; remaining=%d',
                dj.id,
                max(0, pre_filter_count - len(releases)),
                pass_index,
                len(releases),
            )
        if excluded_protocols:
            pre_protocol_filter_count = len(releases)
            releases = [
                release
                for release in releases
                if str(release.get('protocol') or '').strip().lower() not in excluded_protocols
            ]
            logger.info(
                'Download job %s: excluded protocol(s) %s on pass %d; removed=%d remaining=%d',
                dj.id,
                sorted(excluded_protocols),
                pass_index,
                max(0, pre_protocol_filter_count - len(releases)),
                len(releases),
            )
        pre_title_filter_count = len(releases)
        releases = [
            release for release in releases
            if _release_matches_source_title(str(release.get('title') or ''), dj.source_file_path)
        ]
        if pre_title_filter_count != len(releases):
            logger.info(
                'Download job %s: excluded %d release(s) by source-title relevance on pass %d; remaining=%d',
                dj.id,
                pre_title_filter_count - len(releases),
                pass_index,
                len(releases),
            )

        best = _select_best_release(
            releases,
            profile,
            qbt_enabled=qbt.enabled,
            sab_enabled=sab.enabled,
            indexer_by_id=indexer_by_id,
            source_path=dj.source_file_path,
        )
        if best is not None:
            matched_query = query
            break

    if best is None:
        logger.info('Download job %s: no matching release found; skipping grab and falling back to encode', dj.id)
        _fallback_to_encode(db, dj, library, profile)
        return
    if dj.search_query != matched_query:
        dj.search_query = matched_query
        db.commit()

    release_title = str(best.get('title') or '').strip()
    indexer_id_raw = best.get('indexerId')
    try:
        indexer_id = int(indexer_id_raw) if indexer_id_raw is not None else None
    except (TypeError, ValueError):
        indexer_id = None
    indexer_name = str(best.get('_indexer_name') or best.get('indexer') or '').strip() or None
    selected_release_key = _release_selection_key_from_release(best)

    identity_blocker = _download_job_identity_blocker(db, dj)
    if identity_blocker is not None:
        _remove_duplicate_unstarted_download_job(db, dj, identity_blocker)
        return

    if str(best.get('protocol') or '').strip().lower() == 'torrent' and qbt.enabled:
        existing_torrent = _find_existing_qbt_torrent_for_release(
            best,
            download_client_service.get_all_qbt_torrents(qbt),
        )
        if existing_torrent is not None:
            existing_hash = str(existing_torrent.get('hash') or '').strip().lower()
            if existing_hash:
                logger.warning(
                    'Download job %s: reusing existing qBit torrent hash=%s for release %r instead of re-grabbing',
                    dj.id,
                    existing_hash,
                    release_title,
                )
                dj.indexer_id = indexer_id
                dj.indexer_name = indexer_name
                dj.release_name = release_title or None
                dj.selected_release_key = selected_release_key
                dj.download_hash = existing_hash
                dj.client_type = 'qbittorrent'
                dj.status = DownloadJobStatus.downloading.value
                dj.error_message = None
                dj.download_started_at = dj.download_started_at or datetime.now(timezone.utc)
                dj.progress_percent = 0
                dj.eta_seconds = None
                dj.download_speed_bps = None
                db.commit()
                db.refresh(dj)
                _publish_download_job(dj)
                if download_client_service.tag_qbt_torrent(qbt, existing_hash):
                    _tagged_job_ids.add(dj.id)
                return

    if str(best.get('protocol') or '').strip().lower() == 'usenet' and sab.enabled and release_title:
        existing_nzo = download_client_service.find_sab_nzo_for_release(sab, release_title)
        if existing_nzo:
            logger.warning(
                'Download job %s: reusing existing SABnzbd NZO %s for release %r instead of re-grabbing',
                dj.id,
                existing_nzo,
                release_title,
            )
            dj.indexer_id = indexer_id
            dj.indexer_name = indexer_name
            dj.release_name = release_title or None
            dj.selected_release_key = selected_release_key
            dj.download_hash = existing_nzo
            dj.client_type = 'sabnzbd'
            dj.status = DownloadJobStatus.downloading.value
            dj.error_message = None
            dj.download_started_at = dj.download_started_at or datetime.now(timezone.utc)
            dj.progress_percent = 0
            dj.eta_seconds = None
            dj.download_speed_bps = None
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
            if download_client_service.set_sab_category(sab, existing_nzo, category='optimizarr'):
                _categorized_sab_job_ids.add(dj.id)
            return

    logger.info(
        'Download job %s: grabbing release %r (protocol=%s)',
        dj.id,
        best.get('title'),
        str(best.get('protocol', '') or '').lower() or 'unknown',
    )
    grab_result = prowlarr_service.grab(
        prowlarr,
        best.get('guid', ''),
        best.get('indexerId', 0),
    )

    if grab_result is None:
        _retry_failed_download(
            db,
            dj,
            library,
            profile,
            reason='Prowlarr grab failed',
            failed_release_key=_release_selection_key_from_release(best),
        )
        return

    raw_download_hash_candidates = [
        grab_result.get('downloadId'),  # infohash returned by the download client when available
        grab_result.get('hash'),        # secondary hash field used by some Prowlarr versions
        grab_result.get('infoHash'),
        grab_result.get('magnetUrl'),
        grab_result.get('downloadUrl'),
        grab_result.get('guid'),
        _extract_hash_from_release(best),  # hash/magnet data from search payload
        _extract_hash_from_guid(grab_result.get('guid', '')),  # hash embedded in GUID URL
        # downloadClientId and id are intentionally excluded because neither is a torrent infohash.
    ]
    # Store the release name (torrent name as reported by Prowlarr) so that
    # hash recovery can find the torrent in qBit by exact name instead of
    # relying on imprecise timestamp filtering.
    dj.indexer_id = indexer_id
    dj.indexer_name = indexer_name
    dj.release_name = release_title or None
    dj.selected_release_key = selected_release_key

    client_type = _client_type_for_protocol(best.get('protocol', ''))
    if client_type == 'qbittorrent':
        download_hash = ''
        for candidate in raw_download_hash_candidates:
            download_hash = _extract_qbt_info_hash(candidate)
            if download_hash:
                break
    else:
        download_hash = next((str(candidate) for candidate in raw_download_hash_candidates if candidate), '')

    if not download_hash:
        # Prowlarr grab succeeded but returned no client download id/hash.
        # Do NOT fall back to encode — that would unblock the serial constraint
        # and allow additional grabs while the current item is untracked.
        # Keep status=downloading so queue/search stays blocked; progress checks
        # will recover the client-side identifier.
        logger.warning(
            'Download job %s: grab succeeded but no download id/hash returned; '
            'will attempt client-side recovery using release %r. result=%s',
            dj.id, release_title, json.dumps(grab_result)[:200],
        )
        dj.download_hash = None
        dj.client_type = client_type
        dj.status = DownloadJobStatus.downloading.value
        dj.error_message = None
        dj.download_started_at = datetime.now(timezone.utc)
        dj.progress_percent = 0
        dj.eta_seconds = None
        dj.download_speed_bps = None
        db.commit()
        db.refresh(dj)

        # Immediate best-effort recovery so progress/tagging can start without
        # waiting for a later poll cycle.
        if client_type == 'qbittorrent' and qbt.enabled:
            recovered = _recover_qbt_hash_for_job(dj, download_client_service.get_all_qbt_torrents(qbt))
            if recovered:
                dj.download_hash = recovered
                db.commit()
                db.refresh(dj)
                logger.info('Download job %s: recovered qBit hash immediately after grab: %s', dj.id, recovered)
                if download_client_service.tag_qbt_torrent(qbt, recovered):
                    _tagged_job_ids.add(dj.id)
                _reconcile_duplicate_qbt_downloads(db, qbt)
        elif client_type == 'sabnzbd' and sab.enabled and dj.release_name:
            recovered = download_client_service.find_sab_nzo_for_release(sab, dj.release_name)
            if recovered:
                dj.download_hash = recovered
                db.commit()
                db.refresh(dj)
                logger.info('Download job %s: recovered SABnzbd NZO id immediately after grab: %s', dj.id, recovered)
                if download_client_service.set_sab_category(sab, recovered, category='optimizarr'):
                    _categorized_sab_job_ids.add(dj.id)
                _reconcile_duplicate_sab_downloads(db, sab)

        _publish_download_job(dj)
        return

    # qBittorrent stores hashes in lowercase; normalise so lookups always match.
    normalised_hash = str(download_hash).lower() if client_type == 'qbittorrent' else str(download_hash)
    dj.download_hash = normalised_hash
    dj.client_type = client_type
    dj.status = DownloadJobStatus.downloading.value
    dj.error_message = None
    dj.download_started_at = datetime.now(timezone.utc)
    dj.progress_percent = 0
    dj.eta_seconds = None
    dj.download_speed_bps = None
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    logger.info('Download job %s now downloading via %s; hash=%s', dj.id, client_type, dj.download_hash)

    # Tag the torrent in qBittorrent so it's identifiable in the client UI.
    # Use the already-normalised hash to be consistent with what is stored in the DB.
    if client_type == 'qbittorrent' and qbt.enabled:
        if download_client_service.tag_qbt_torrent(qbt, normalised_hash):
            _tagged_job_ids.add(dj.id)
        _reconcile_duplicate_qbt_downloads(db, qbt)
    elif client_type == 'sabnzbd' and sab.enabled and dj.download_hash:
        if download_client_service.set_sab_category(sab, dj.download_hash, category='optimizarr'):
            _categorized_sab_job_ids.add(dj.id)
        _reconcile_duplicate_sab_downloads(db, sab)


# ─────────────────────────────────────────────────────────────────────────────
# Processing: downloading → complete/failed/timed_out
# ─────────────────────────────────────────────────────────────────────────────

def _process_downloading_jobs(db: Session) -> None:
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)
    _cleanup_stale_qbt_torrents(db, qbt)
    _reconcile_duplicate_qbt_downloads(db, qbt)
    _reconcile_duplicate_sab_downloads(db, sab)

    jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_([
            DownloadJobStatus.queued.value,
            DownloadJobStatus.downloading.value,
            DownloadJobStatus.moving.value,
        ]))
        .all()
    )

    for dj in jobs:
        _check_download_progress(db, dj, qbt, sab)


def _check_download_progress(db: Session, dj: DownloadJob, qbt, sab) -> None:
    library = db.query(Library).filter(Library.id == dj.library_id).first()
    if library is None or library.profile is None:
        _mark_failed(db, dj, 'Library or profile not found')
        return

    profile = library.profile
    client_type = dj.client_type or 'qbittorrent'

    invalid_qbt_hash = False
    if client_type == 'qbittorrent' and dj.download_hash and not _normalize_qbt_info_hash(dj.download_hash):
        logger.warning(
            'Download job %s: stored qBit hash %r is not a valid infohash; recovering from qBit by release/source',
            dj.id,
            dj.download_hash,
        )
        invalid_qbt_hash = True
        dj.download_hash = None
        db.commit()
        db.refresh(dj)

    # If the hash is unknown (grab succeeded but Prowlarr returned no hash),
    # look up the torrent in qBit using the release name stored at grab time.
    if not dj.download_hash and client_type == 'qbittorrent' and qbt.enabled:
        all_torrents = download_client_service.get_all_qbt_torrents(qbt)

        recovered: dict | None = None

        if dj.release_name:
            # Primary strategy: exact name match against the release title
            # Prowlarr sent to qBit.  This is unambiguous regardless of how many
            # other torrents exist in the client.
            name_matches = [t for t in all_torrents if t.get('name') == dj.release_name]
            if len(name_matches) == 1:
                recovered = name_matches[0]
                logger.info(
                    'Download job %s: recovered hash via release name %r',
                    dj.id, dj.release_name,
                )
            elif len(name_matches) > 1:
                # Duplicate torrents with the same name — pick the newest one.
                recovered = max(name_matches, key=lambda t: t.get('added_on', 0))
                logger.info(
                    'Download job %s: %d torrents named %r; picked newest',
                    dj.id, len(name_matches), dj.release_name,
                )
            else:
                # qBit names can differ from the original indexer title (e.g.
                # post-grab metadata normalization). Try fuzzy matching before
                # giving up and waiting.
                recovered = _find_qbt_torrent_for_release(dj, all_torrents)
                if recovered is not None:
                    logger.info(
                        'Download job %s: recovered hash via fuzzy release match for %r',
                        dj.id, dj.release_name,
                    )
                else:
                    if invalid_qbt_hash:
                        _retry_failed_download(
                            db,
                            dj,
                            library,
                            profile,
                            reason='Stored qBittorrent hash was invalid and no matching torrent was found',
                            failed_release_key=_release_selection_key_from_job(dj),
                        )
                        return
                    logger.debug(
                        'Download job %s: release %r not found in qBit yet; waiting',
                        dj.id, dj.release_name,
                    )
                    return
        else:
            # Fallback for jobs grabbed before release_name was stored: use
            # timestamp filtering as before.
            job_ts = dj.created_at.replace(tzinfo=timezone.utc).timestamp()
            recent = [t for t in all_torrents if t.get('added_on', 0) >= job_ts]
            if not recent:
                if invalid_qbt_hash:
                    _retry_failed_download(
                        db,
                        dj,
                        library,
                        profile,
                        reason='Stored qBittorrent hash was invalid and no recent torrent was found',
                        failed_release_key=_release_selection_key_from_job(dj),
                    )
                    return
                logger.debug('Download job %s: no recent qBit torrent found yet; waiting', dj.id)
                return
            if len(recent) == 1:
                recovered = recent[0]
            else:
                logger.warning(
                    'Download job %s: %d recent qBit torrents and no release_name to disambiguate — waiting',
                    dj.id, len(recent),
                )
                recovered_hash = _recover_qbt_hash_for_job(dj, all_torrents)
                if not recovered_hash:
                    if invalid_qbt_hash:
                        _retry_failed_download(
                            db,
                            dj,
                            library,
                            profile,
                            reason='Stored qBittorrent hash was invalid and no matching torrent was found',
                            failed_release_key=_release_selection_key_from_job(dj),
                        )
                        return
                    return
                recovered = next((t for t in all_torrents if str(t.get('hash', '')).lower() == recovered_hash), None)
                if recovered is None:
                    return

        recovered_hash = recovered['hash'].lower()
        logger.info('Download job %s: recovered hash %s', dj.id, recovered_hash)
        dj.download_hash = recovered_hash
        db.commit()
        db.refresh(dj)
        if download_client_service.tag_qbt_torrent(qbt, recovered_hash):
            _tagged_job_ids.add(dj.id)

    # If SAB grab succeeded but Prowlarr didn't return NZO id, recover by
    # matching release name against queue/history and wait until it appears.
    if not dj.download_hash and client_type == 'sabnzbd' and sab.enabled:
        recovered_item = _find_sab_queue_item_for_download_job(dj, sab)
        recovered_nzo = str(recovered_item.get('nzo_id') or '').strip() if recovered_item else ''
        if not recovered_nzo and dj.release_name:
            recovered_nzo = download_client_service.find_sab_nzo_for_release(sab, dj.release_name)
        if recovered_nzo:
            dj.download_hash = recovered_nzo
            db.commit()
            db.refresh(dj)
            logger.info('Download job %s: recovered SABnzbd NZO id %s', dj.id, recovered_nzo)
            if download_client_service.set_sab_category(sab, recovered_nzo, category='optimizarr'):
                _categorized_sab_job_ids.add(dj.id)
        else:
            if dj.release_name:
                logger.debug(
                    'Download job %s: SABnzbd NZO id not found yet for release %r; waiting',
                    dj.id,
                    dj.release_name,
                )
                return
            else:
                logger.debug('Download job %s: no release_name for SABnzbd hash recovery yet; waiting', dj.id)
                return

    # If the initial tag attempt failed (e.g. torrent wasn't indexed in qBit
    # yet when we grabbed it), retry with a few quick attempts each monitoring
    # loop until confirmed.
    if dj.id not in _tagged_job_ids and client_type == 'qbittorrent' and qbt.enabled and dj.download_hash:
        if download_client_service.tag_qbt_torrent(qbt, dj.download_hash, max_attempts=3):
            _tagged_job_ids.add(dj.id)
    if dj.id not in _categorized_sab_job_ids and client_type == 'sabnzbd' and sab.enabled and dj.download_hash:
        if download_client_service.set_sab_category(sab, dj.download_hash, category='optimizarr'):
            _categorized_sab_job_ids.add(dj.id)

    if client_type == 'qbittorrent' and qbt.enabled and _qbt_hash_mismatches_tv_download_job(dj, qbt):
        logger.warning(
            'Download job %s: qBittorrent hash %s belongs to a different TV episode; unlinking and retrying search',
            dj.id,
            dj.download_hash,
        )
        dj.download_hash = None
        dj.status = DownloadJobStatus.searching.value
        dj.progress_percent = 0
        dj.eta_seconds = None
        dj.download_speed_bps = None
        dj.error_message = 'qBittorrent item belongs to a different episode; retrying search'
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        return

    if client_type == 'sabnzbd' and sab.enabled and _sab_nzo_mismatches_tv_download_job(dj, sab):
        logger.warning(
            'Download job %s: SAB NZO %s belongs to a different TV episode; unlinking and retrying search',
            dj.id,
            dj.download_hash,
        )
        dj.download_hash = None
        dj.status = DownloadJobStatus.searching.value
        dj.progress_percent = 0
        dj.eta_seconds = None
        dj.download_speed_bps = None
        dj.error_message = 'SABnzbd item belongs to a different episode; retrying search'
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        return

    status = download_client_service.get_download_status(client_type, qbt, sab, dj.download_hash or '')

    # qBittorrent can occasionally return a stale hash from the grab response.
    # If the lookup appears missing (0% and not complete), try re-matching by
    # release name and continue tracking the real torrent instead of appearing
    # stuck at 0% indefinitely.
    if (
        client_type == 'qbittorrent'
        and qbt.enabled
        and dj.download_hash
        and not status.get('is_complete')
        and not status.get('is_moving')
        and not status.get('is_waiting')
        and int(status.get('progress_percent', 0) or 0) == 0
    ):
        all_torrents = download_client_service.get_all_qbt_torrents(qbt)
        replacement = _find_qbt_torrent_for_release(dj, all_torrents)
        if replacement and replacement.get('hash'):
            replacement_hash = replacement['hash'].lower()
            if replacement_hash != dj.download_hash:
                logger.warning(
                    'Download job %s: replacing stale qBit hash %s with %s via release-name match',
                    dj.id, dj.download_hash, replacement_hash,
                )
                dj.download_hash = replacement_hash
                _tagged_job_ids.discard(dj.id)
                db.commit()
                db.refresh(dj)
                if download_client_service.tag_qbt_torrent(qbt, replacement_hash, max_attempts=3):
                    _tagged_job_ids.add(dj.id)
            status = {
                'progress_percent': int((replacement.get('progress', 0) or 0) * 100),
                'eta_seconds': replacement.get('eta'),
                'download_speed_bps': replacement.get('dlspeed'),
                'is_complete': replacement.get('state', '') in download_client_service._QBT_COMPLETE_STATES,
                'is_moving': replacement.get('state', '') in download_client_service._QBT_MOVING_STATES,
                'is_waiting': replacement.get('state', '') in download_client_service._QBT_WAITING_STATES,
                'is_stalled': replacement.get('state', '') in ('stalledDL', 'missingFiles', 'error', 'stoppedDL'),
                'qbt_state': replacement.get('state', ''),
                'save_path': replacement.get('content_path') or replacement.get('save_path'),
                'not_found': False,
            }

    # If the tracked item was removed/not found in the client, retry by
    # searching for another release instead of immediately falling back.
    if status.get('not_found'):
        if client_type == 'qbittorrent' and qbt.enabled:
            all_torrents = download_client_service.get_all_qbt_torrents(qbt)
            replacement = _find_qbt_torrent_for_release(dj, all_torrents)
            if replacement and replacement.get('hash'):
                replacement_hash = str(replacement.get('hash') or '').strip().lower()
                logger.warning(
                    'Download job %s: qBit lookup missed torrent %s; recovered existing torrent %s via release match',
                    dj.id,
                    dj.download_hash,
                    replacement_hash,
                )
                dj.download_hash = replacement_hash
                _tagged_job_ids.discard(dj.id)
                db.commit()
                db.refresh(dj)
                if download_client_service.tag_qbt_torrent(qbt, replacement_hash, max_attempts=3):
                    _tagged_job_ids.add(dj.id)
                status = {
                    'progress_percent': int((replacement.get('progress', 0) or 0) * 100),
                    'eta_seconds': replacement.get('eta'),
                    'download_speed_bps': replacement.get('dlspeed'),
                    'is_complete': replacement.get('state', '') in download_client_service._QBT_COMPLETE_STATES,
                    'is_moving': replacement.get('state', '') in download_client_service._QBT_MOVING_STATES,
                    'is_waiting': replacement.get('state', '') in download_client_service._QBT_WAITING_STATES,
                    'is_stalled': replacement.get('state', '') in ('stalledDL', 'missingFiles', 'error', 'stoppedDL'),
                    'qbt_state': replacement.get('state', ''),
                    'save_path': replacement.get('content_path') or replacement.get('save_path'),
                    'not_found': False,
                }
        elif client_type == 'sabnzbd' and sab.enabled:
            replacement_item = _find_sab_queue_item_for_download_job(dj, sab)
            replacement_nzo = str(replacement_item.get('nzo_id') or '').strip() if replacement_item else ''
            if not replacement_nzo and dj.release_name:
                replacement_nzo = download_client_service.find_sab_nzo_for_release(sab, dj.release_name)
            if replacement_nzo:
                logger.warning(
                    'Download job %s: SAB lookup missed NZO %s; recovered existing NZO %s via release match',
                    dj.id,
                    dj.download_hash,
                    replacement_nzo,
                )
                dj.download_hash = replacement_nzo
                _categorized_sab_job_ids.discard(dj.id)
                db.commit()
                db.refresh(dj)
                if download_client_service.set_sab_category(sab, replacement_nzo, category='optimizarr'):
                    _categorized_sab_job_ids.add(dj.id)
                status = download_client_service.get_download_status(client_type, qbt, sab, replacement_nzo)
                if status.get('not_found'):
                    status = {
                        'progress_percent': int(dj.progress_percent or 0),
                        'eta_seconds': dj.eta_seconds,
                        'download_speed_bps': dj.download_speed_bps,
                        'is_complete': False,
                        'is_moving': False,
                        'is_waiting': True,
                        'is_stalled': False,
                        'sab_status': 'Recovered',
                        'save_path': None,
                        'not_found': False,
                    }
        if status.get('not_found'):
            if _client_tracking_grace_active(dj):
                logger.info(
                    'Download job %s: %s item not visible yet; waiting within %ss tracking grace before retrying',
                    dj.id,
                    client_type,
                    _CLIENT_TRACKING_GRACE_SECONDS,
                )
                return
            _retry_failed_download(
                db,
                dj,
                library,
                profile,
                reason=f'Removed from {client_type}',
                failed_release_key=_release_selection_key_from_job(dj),
            )
            return
    progress = status.get('progress_percent', 0)
    try:
        progress = int(float(progress))
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(100, progress))

    is_complete = bool(status.get('is_complete'))
    is_moving = bool(status.get('is_moving')) and not is_complete
    if is_moving and progress == 0 and int(dj.progress_percent or 0) > 0:
        # Clients can report 0% while post-download file moves are in progress.
        # Keep the last known progress to avoid UI regressions/jumps.
        progress = int(dj.progress_percent or 0)

    eta_seconds = status.get('eta_seconds')
    eta_seconds = int(eta_seconds) if isinstance(eta_seconds, (int, float)) and eta_seconds >= 0 else None
    speed_bps = status.get('download_speed_bps')
    speed_bps = int(speed_bps) if isinstance(speed_bps, (int, float)) and speed_bps >= 0 else None

    # Some clients, especially SABnzbd around slot changes/recovery, can report
    # a waiting/queued status while bytes are already moving. Treat nonzero
    # progress as active so the app does not show a downloading item as queued.
    is_waiting = bool(status.get('is_waiting')) and client_type in {'qbittorrent', 'sabnzbd'} and progress <= 0
    if is_waiting:
        expected_status = DownloadJobStatus.queued.value
        eta_seconds = None
        speed_bps = 0
    else:
        expected_status = DownloadJobStatus.moving.value if is_moving else DownloadJobStatus.downloading.value
    should_update = (
        progress != dj.progress_percent
        or eta_seconds != dj.eta_seconds
        or speed_bps != dj.download_speed_bps
        or dj.status != expected_status
    )
    if should_update:
        dj.status = expected_status
        dj.progress_percent = progress
        dj.eta_seconds = eta_seconds
        dj.download_speed_bps = speed_bps
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)

    # Always check for completion BEFORE applying the timeout.  A download that
    # finished just as the deadline elapsed should be imported, not discarded.
    if is_complete:
        if dj.eta_seconds != 0:
            dj.eta_seconds = 0
            dj.download_speed_bps = 0
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
        save_path = status.get('save_path')
        if save_path:
            logger.info('Download job %s complete; save_path=%r', dj.id, save_path)
            _import_file(db, dj, save_path, library, profile, qbt, sab)
        else:
            _mark_failed(db, dj, 'Download marked complete but no save path returned')
            _fallback_to_encode(db, dj, library, profile)
        return

    if status.get('is_stalled') and client_type == 'qbittorrent':
        # qBittorrent can report new or metadata-resolving torrents as
        # stalledDL temporarily even though they later resume and complete.
        # Keep tracking the same job and rely on the normal timeout path for
        # genuinely long-lived stalls so the eventual completed item can still
        # be imported.
        logger.info(
            'Download job %s: qBittorrent reported stalled state; continuing to monitor before retrying',
            dj.id,
        )

    elif status.get('is_stalled'):
        _retry_failed_download(
            db,
            dj,
            library,
            profile,
            reason=f'Download stalled in {client_type}',
            failed_release_key=_release_selection_key_from_job(dj),
        )
        return

    if is_waiting:
        # Client-side queueing is intentional backpressure, not a bad release.
        # Keep the timeout clock parked while the client has not started it yet.
        dj.download_started_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(dj)
        state_label = status.get('qbt_state') or status.get('sab_status') or 'queued'
        logger.info(
            'Download job %s: %s state=%s; waiting in client queue without consuming retry timeout',
            dj.id,
            client_type,
            state_label,
        )
        return

    # Timeout check: only applied when the download is not yet complete.
    # Use download_started_at (when the torrent was sent to the client) as the
    # timeout reference so that jobs created in a previous run don't immediately
    # time out when the app restarts.  Fall back to created_at for rows that
    # pre-date the download_started_at column.
    timeout_minutes = int(getattr(profile, 'download_timeout_minutes', 60) or 60)
    timeout_reference = dj.download_started_at or dj.created_at
    if timeout_reference.tzinfo is None:
        timeout_reference = timeout_reference.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - timeout_reference
    if elapsed > timedelta(minutes=timeout_minutes):
        _retry_failed_download(
            db,
            dj,
            library,
            profile,
            reason=f'Download timed out after {timeout_minutes} minutes',
            failed_release_key=_release_selection_key_from_job(dj),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Import: move downloaded file from complete dir to library
# ─────────────────────────────────────────────────────────────────────────────

def _cleanup_replaced_after_download_import(db: Session, dj: DownloadJob) -> tuple[int, list[int]]:
    removed_files, affected_job_ids, affected_download_job_ids = cleanup_replaced_optimized_outputs(
        db,
        library_id=dj.library_id,
        source_path=dj.source_file_path,
        keep_output_path=dj.imported_file_path,
        current_download_job_id=dj.id,
    )
    if removed_files:
        logger.info(
            'Removed %s replaced artifact(s) for completed download job %s; affected_jobs=%s',
            removed_files,
            dj.id,
            affected_job_ids + affected_download_job_ids,
        )
    return removed_files, affected_download_job_ids


def _import_file(db: Session, dj: DownloadJob, save_path: str, library: Library, profile: LibraryProfile, qbt, sab) -> None:
    """
    Import a completed download into the library.

    The download client moves files from its incomplete directory to its
    complete directory automatically.  Optimizarr only needs access to the
    complete directory; it reads the video file from there and moves/copies
    it to the library's media folder.
    """
    dj.status = DownloadJobStatus.importing.value
    dj.eta_seconds = None
    dj.download_speed_bps = None
    db.commit()
    _publish_download_job(dj)

    video_file = download_client_service.find_video_in_path(save_path)
    if video_file is None:
        logger.error('Download job %s: no video file found in %r', dj.id, save_path)
        _cleanup_download_client(dj, qbt, sab, save_path=save_path, delete_files=True)
        _retry_failed_download(
            db,
            dj,
            library,
            profile,
            reason=f'No video file found in {save_path}',
            failed_release_key=_release_selection_key_from_job(dj),
        )
        return

    dj.downloaded_file_path = str(video_file)
    db.commit()

    # Build destination path mirroring encoding output naming
    source = Path(dj.source_file_path)
    container = str(profile.container.value if hasattr(profile.container, 'value') else profile.container).lower().strip('.')
    dest = source.with_name(f'{source.stem}{profile.output_suffix}.{container}')

    # Apply output conflict policy
    policy = str(profile.output_conflict_policy.value if hasattr(profile.output_conflict_policy, 'value') else profile.output_conflict_policy).lower()
    if dest.exists():
        if policy == 'overwrite':
            dest.unlink(missing_ok=True)
        elif policy == 'rename':
            version = 2
            while dest.exists():
                dest = source.with_name(f'{source.stem}{profile.output_suffix}-v{version}.{container}')
                version += 1
        else:  # skip
            logger.info('Download job %s: output already exists and policy=skip; marking complete without import', dj.id)
            dj.status = DownloadJobStatus.complete.value
            dj.imported_file_path = str(dest)
            dj.eta_seconds = None
            dj.download_speed_bps = None
            dj.completed_at = datetime.now(timezone.utc)
            _, affected_download_job_ids = _cleanup_replaced_after_download_import(db, dj)
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
            for affected_download_job in db.query(DownloadJob).filter(DownloadJob.id.in_(affected_download_job_ids)).all():
                _publish_download_job(affected_download_job)
            notification_service.enqueue_download_job_complete(dj)
            _cleanup_download_client(dj, qbt, sab, save_path=save_path, delete_files=True)
            return

    # Place the downloaded file into the library.
    # Strategy differs by download client:
    #   qBittorrent — hard-link so both the library and the seed directory
    #                 reference the same inode; the original is never removed,
    #                 allowing qBit to keep seeding.  Falls back to a copy
    #                 (without deleting the source) if the library and the seed
    #                 dir are on different filesystems.
    #   SABnzbd     — move (rename on same fs, copy+delete across filesystems);
    #                 SABnzbd manages its own files so removing the original
    #                 is correct here.
    dest.parent.mkdir(parents=True, exist_ok=True)
    is_qbt = dj.client_type == 'qbittorrent'
    try:
        if is_qbt:
            try:
                os.link(video_file, dest)
                logger.info('Download job %s: hard linked %r → %r', dj.id, str(video_file), str(dest))
            except OSError:
                # Cross-device or unsupported fs — copy without deleting source
                temp_dest = dest.parent / f'.optimizarr-import-{dj.id}-{int(time.time() * 1000)}{dest.suffix}'
                shutil.copy2(video_file, temp_dest)
                os.replace(temp_dest, dest)
                logger.info('Download job %s: copied %r → %r (cross-device, original kept for seeding)',
                            dj.id, str(video_file), str(dest))
        else:
            if _is_same_filesystem(video_file, dest.parent):
                try:
                    os.replace(video_file, dest)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    temp_dest = dest.parent / f'.optimizarr-import-{dj.id}-{int(time.time() * 1000)}{dest.suffix}'
                    shutil.copy2(video_file, temp_dest)
                    os.replace(temp_dest, dest)
                    video_file.unlink(missing_ok=True)
                    logger.info(
                        'Download job %s: copied %r → %r after cross-device rename failure',
                        dj.id,
                        str(video_file),
                        str(dest),
                    )
            else:
                temp_dest = dest.parent / f'.optimizarr-import-{dj.id}-{int(time.time() * 1000)}{dest.suffix}'
                shutil.copy2(video_file, temp_dest)
                os.replace(temp_dest, dest)
                video_file.unlink(missing_ok=True)
            _cleanup_sab_import_source(save_path, video_file)
    except OSError as exc:
        logger.error('Download job %s: failed to import %r → %r: %s', dj.id, video_file, dest, exc)
        _cleanup_download_client(dj, qbt, sab, save_path=save_path, delete_files=True)
        _mark_failed(db, dj, f'File import failed: {exc}')
        _stop_download_queue(f'Import error for job {dj.id}: {exc}')
        _fallback_to_encode(db, dj, library, profile)
        return

    logger.info('Download job %s: imported %r → %r', dj.id, str(video_file), str(dest))
    dj.imported_file_path = str(dest)

    # Post-import validation: check if HDR tone-mapping is needed
    if getattr(profile, 'tone_map_hdr', False) and is_hdr_video(str(dest)):
        logger.info('Download job %s: imported file is HDR and tone_map_hdr=True; queuing encode', dj.id)
        try:
            height = probe_video_height(str(dest))
            encode_job = create_job(
                db,
                str(dest),
                library_id=library.id,
                profile=profile,
                source_resolution=height,
                source_is_hdr=True,
            )
            dj.encode_job_id = encode_job.id
        except Exception as exc:
            logger.warning('Download job %s: failed to create tone-map encode job: %s', dj.id, exc)

    dj.status = DownloadJobStatus.complete.value
    dj.eta_seconds = None
    dj.download_speed_bps = None
    dj.completed_at = datetime.now(timezone.utc)
    _, affected_download_job_ids = _cleanup_replaced_after_download_import(db, dj)
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    for affected_download_job in db.query(DownloadJob).filter(DownloadJob.id.in_(affected_download_job_ids)).all():
        _publish_download_job(affected_download_job)
    notification_service.enqueue_download_job_complete(dj)
    _link_completed_downloads_to_waiting_jobs(db)

    # Clean up the download client entry after a successful import
    _cleanup_download_client(dj, qbt, sab, save_path=save_path, delete_files=True)

    # Notify Plex if configured
    try:
        from app.services.plex_service import trigger_scan_after_job
        trigger_scan_after_job(library.id)
    except Exception:
        pass

    # Wake the monitor immediately so the next queued search can begin without
    # waiting for the full idle poll interval.
    _wake_event.set()

    # Notify the discovery service so it can immediately scan for the next
    # eligible file rather than waiting for the next scheduled interval.
    if _on_job_complete:
        try:
            _on_job_complete()
        except Exception:
            pass


def _cleanup_download_client(
    dj: DownloadJob,
    qbt,
    sab,
    *,
    save_path: str | None = None,
    delete_files: bool = False,
) -> None:
    """
    Post-import client cleanup:
    - SABnzbd: remove the queue/history entry and optionally request file deletion.
    - qBittorrent: leave untouched so it can follow its own seeding rules.
    """
    if dj.client_type == 'sabnzbd' and sab is not None and dj.download_hash:
        if delete_files:
            removed = download_client_service.remove_sab_job(sab, dj.download_hash, delete_files=True)
            if not removed:
                download_client_service.delete_sab_history(sab, dj.download_hash)
        else:
            download_client_service.delete_sab_history(sab, dj.download_hash)

    if delete_files and dj.client_type == 'sabnzbd' and save_path:
        _purge_sab_completed_path(save_path)


def _purge_sab_completed_path(save_path: str) -> None:
    """Best-effort removal of a SAB completed path (file or per-release directory)."""
    root = Path(str(save_path or '').strip())
    if not str(root):
        return
    try:
        if root.is_file():
            root.unlink(missing_ok=True)
            return
        if root.is_dir():
            # Guard against accidental deletion of very broad roots.
            name = root.name.lower()
            if name in {'', '.', '..', 'complete', 'downloads', 'nzb'}:
                logger.warning('SAB cleanup skipped unsafe directory path: %r', str(root))
                return
            shutil.rmtree(root)
    except OSError as exc:
        logger.warning('SAB cleanup failed for %r: %s', str(root), exc)


def _cleanup_sab_import_source(save_path: str, imported_source_file: Path) -> None:
    """Ensure SAB/Usenet source files are removed from completed storage.

    The import path already does move semantics for SAB, but this extra cleanup
    guarantees the imported video is not left behind in completed folders when
    edge cases occur. Empty parent folders are pruned conservatively.
    """
    if imported_source_file.exists():
        try:
            imported_source_file.unlink(missing_ok=True)
        except OSError:
            logger.debug('SAB import cleanup: could not remove source file %r', str(imported_source_file))

    root = Path(save_path)
    boundary = root if root.is_dir() else root.parent
    current = imported_source_file.parent
    while current and current.exists() and current.is_dir():
        try:
            current.rmdir()
        except OSError:
            break
        if current == boundary:
            break
        current = current.parent


def _is_same_filesystem(file_path: Path, target_dir: Path) -> bool:
    try:
        return os.stat(file_path).st_dev == os.stat(target_dir).st_dev
    except OSError:
        return False


def _normalize_release_key(value: str | None) -> str:
    if not value:
        return ''
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def _build_completed_root_match_keys(dj: DownloadJob) -> list[str]:
    """Build broad-to-specific keys for completed-root directory matching.

    The persisted source path often includes metadata tags (imdb id, codec,
    quality blocks) that are not present in the qBit completed folder name.
    Include simplified title/year variants so restarts can still recover files
    like:
      source: Doctor Strange (2016) {imdb-...} [Bluray-2160p]...
      folder: Doctor Strange (2016) IMAX (1080p ...)
    """
    keys: list[str] = []

    # Existing keys.
    keys.append(_normalize_release_key(dj.release_name))

    source_stem = Path(dj.source_file_path).stem if dj.source_file_path else ''
    keys.append(_normalize_release_key(source_stem))

    # Remove bracketed metadata blocks before title/year extraction.
    clean = re.sub(r'\{[^}]*\}', ' ', source_stem)
    clean = re.sub(r'\[[^\]]*\]', ' ', clean)
    clean = re.sub(r'[._-]+', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    year_match = re.search(r'\b(19|20)\d{2}\b', clean)
    if year_match:
        year = year_match.group(0)
        title = re.sub(r'[\s()\[\]{}._-]+$', '', clean[:year_match.start()]).strip()
        title_key = _normalize_release_key(title)
        if title_key:
            keys.append(_normalize_release_key(f'{title} {year}'))
            keys.append(title_key)
    else:
        title_key = _normalize_release_key(clean)
        if title_key:
            keys.append(title_key)

    # De-duplicate while preserving insertion order and prioritize specific
    # keys first to reduce accidental partial matches.
    seen: set[str] = set()
    unique = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    unique.sort(key=len, reverse=True)
    return unique


def _find_qbt_torrent_for_release(dj: DownloadJob, torrents: list[dict]) -> dict | None:
    if not torrents:
        logger.info('Download job %s match: no qBit torrents available for release matching', dj.id)
        return None

    if dj.release_name:
        exact = [t for t in torrents if t.get('name') == dj.release_name]
        if exact:
            # Prefer optimizarr-tagged torrents first, then newest.
            chosen = max(exact, key=lambda t: (1 if _is_optimizarr_tagged(t) else 0, t.get('added_on', 0)))
            logger.info(
                'Download job %s match: exact release-name match (%d candidate(s)); picked hash=%s name=%r',
                dj.id,
                len(exact),
                str(chosen.get('hash', '')).lower(),
                chosen.get('name'),
            )
            return chosen

    candidate_keys = [
        _normalize_release_key(dj.release_name),
        _normalize_release_key(Path(dj.source_file_path).stem),
    ]
    keys = []
    for key in candidate_keys:
        if key and key not in keys:
            keys.append(key)
    if not keys:
        logger.info('Download job %s match: no normalized key available from release/source name', dj.id)
        return None

    for key in keys:
        partial_matches = [
            t for t in torrents
            if key in _normalize_release_key(t.get('name', ''))
        ]
        if partial_matches:
            # Prefer optimizarr-tagged torrents first, then newest.
            chosen = max(partial_matches, key=lambda t: (1 if _is_optimizarr_tagged(t) else 0, t.get('added_on', 0)))
            logger.info(
                'Download job %s match: normalized key=%r matched %d torrent(s); picked hash=%s name=%r',
                dj.id,
                key,
                len(partial_matches),
                str(chosen.get('hash', '')).lower(),
                chosen.get('name'),
            )
            return chosen

    token_sources = [str(dj.release_name or '')]
    source_title, _source_year = _extract_source_title_and_year(dj.source_file_path)
    if source_title:
        token_sources.append(source_title)

    token_matches: list[dict] = []
    quality_tokens = {'480p', '720p', '1080p', '2160p', '4k', 'h264', 'h265', 'x264', 'x265', 'hevc', 'av1'}
    for token_source in token_sources:
        source_tokens = {
            token for token in _title_tokens_for_matching(token_source)
            if token not in quality_tokens and not re.fullmatch(r'(?:19|20)\d{2}', token)
        }
        if len(source_tokens) < 2:
            continue
        for torrent in torrents:
            torrent_tokens = set(_title_tokens_for_matching(str(torrent.get('name') or '')))
            if source_tokens.issubset(torrent_tokens):
                token_matches.append(torrent)

    if token_matches:
        chosen = max(token_matches, key=lambda t: (1 if _is_optimizarr_tagged(t) else 0, t.get('added_on', 0)))
        logger.info(
            'Download job %s match: title-token match (%d candidate(s)); picked hash=%s name=%r',
            dj.id,
            len(token_matches),
            str(chosen.get('hash', '')).lower(),
            chosen.get('name'),
        )
        return chosen

    logger.info(
        'Download job %s match: no qBit name matched normalized keys=%s (release=%r source=%r)',
        dj.id,
        keys,
        dj.release_name,
        dj.source_file_path,
    )
    return None


def _find_completed_download_match(dj: DownloadJob, completed_root: str | None) -> str | None:
    if not completed_root:
        logger.info('Download job %s match: qBit completed root is not configured', dj.id)
        return None

    root = Path(completed_root)
    if not root.exists() or not root.is_dir():
        logger.info('Download job %s match: qBit completed root %r is missing or not a directory', dj.id, completed_root)
        return None

    keys = _build_completed_root_match_keys(dj)
    if not keys:
        logger.info('Download job %s match: no completed-root keys available for path matching', dj.id)
        return None

    try:
        for candidate in root.iterdir():
            name_key = _normalize_release_key(candidate.name)
            if any(k and k in name_key for k in keys):
                found = download_client_service.find_video_in_path(str(candidate))
                if found:
                    logger.info(
                        'Download job %s match: completed-root match candidate=%r keys=%s',
                        dj.id,
                        str(candidate),
                        keys,
                    )
                    return str(candidate)
    except OSError:
        logger.info('Download job %s match: failed reading completed-root %r', dj.id, completed_root)
        return None
    if dj.id is None:
        logger.debug(
            'Download job %s match: no completed-root entry matched keys=%s in %r',
            dj.id,
            keys,
            completed_root,
        )
    else:
        logger.info(
            'Download job %s match: no completed-root entry matched keys=%s in %r',
            dj.id,
            keys,
            completed_root,
        )
    return None


def _release_title_matches_profile(title: str, profile: LibraryProfile) -> bool:
    title = (title or '').strip()
    if not title:
        return False

    release = {'title': title}
    title_lower = title.lower()
    tone_map_hdr = _coerce_bool(getattr(profile, 'tone_map_hdr', False))
    if not _release_matches_target_resolution(release, profile.target_resolution):
        return False
    if tone_map_hdr and _is_hdr_release(title_lower):
        return False

    allowed_codecs = _allowed_download_codecs(profile)
    detected_codecs = _detect_release_codecs(release)
    if detected_codecs and not (detected_codecs & allowed_codecs):
        return False

    quality_val = _quality_profile_value(profile)
    if quality_val == DownloadQualityProfileEnum.any.value:
        return True

    quality_class = _classify_release_quality_from_release(release)
    return quality_class == quality_val


def _reset_download_job_to_searching(db: Session, dj: DownloadJob) -> None:
    dj.status = DownloadJobStatus.searching.value
    dj.indexer_id = None
    dj.indexer_name = None
    dj.selected_release_key = None
    dj.download_hash = None
    dj.client_type = None
    dj.progress_percent = 0
    dj.eta_seconds = None
    dj.download_speed_bps = None
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)


# ─────────────────────────────────────────────────────────────────────────────
# Fallback + failure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_to_encode(db: Session, dj: DownloadJob, library: Library, profile: LibraryProfile) -> None:
    """Create a normal encode job for the original source file."""
    try:
        height = probe_video_height(dj.source_file_path)
        hdr = is_hdr_video(dj.source_file_path)
        encode_job = create_job(
            db,
            dj.source_file_path,
            library_id=library.id,
            profile=profile,
            source_resolution=height,
            source_is_hdr=hdr,
        )
        if dj.encode_job_id is None:
            dj.encode_job_id = encode_job.id
        dj.status = DownloadJobStatus.waiting_encode.value
        dj.eta_seconds = None
        dj.download_speed_bps = None
        dj.completed_at = None
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        broker.publish_notification(
            f'Download failed for {Path(dj.source_file_path).name}; falling back to encode'
        )
        logger.info('Download job %s: fallback encode job %s created', dj.id, encode_job.id)
        if _on_job_complete:
            try:
                _on_job_complete()
            except Exception:
                pass
    except Exception as exc:
        logger.error('Download job %s: failed to create fallback encode job: %s', dj.id, exc)


def _mark_failed(db: Session, dj: DownloadJob, reason: str) -> None:
    dj.status = DownloadJobStatus.failed.value
    dj.error_message = reason
    dj.eta_seconds = None
    dj.download_speed_bps = None
    dj.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    notification_service.enqueue_download_job_failed(dj)
    logger.warning('Download job %s failed: %s', dj.id, reason)


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery
# ─────────────────────────────────────────────────────────────────────────────

def _arm_startup_grace_if_needed(db: Session) -> None:
    """Set the startup grace period if any searching jobs exist in the DB.

    Called from run_download_startup_recovery() before the monitor loop starts
    so that Prowlarr grabs do not fire the instant the app comes up.  Gives the
    operator 60 s to review / delete jobs they do not want to process.
    """
    global _startup_grace_until
    searching_count = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.searching.value)
        .count()
    )
    if searching_count > 0:
        _startup_grace_until = datetime.now(timezone.utc) + timedelta(seconds=_STARTUP_GRACE_SECONDS)
        logger.warning(
            'Download startup recovery: %d searching job(s) found; '
            'holding off grabs for %ds (until %s UTC) — remove unwanted jobs now',
            searching_count, _STARTUP_GRACE_SECONDS, _startup_grace_until.strftime('%H:%M:%S'),
        )


def run_download_startup_recovery(db: Session) -> dict:
    """Reconcile in-flight download jobs against the download client on startup.

    Handles three scenarios that occur when Optimizarr restarts:

    1. A torrent finished downloading while the app was offline — qBittorrent
       has the file ready but we never ran the import step.  We detect this by
       scanning all 'optimizarr'-tagged torrents in qBit (case-insensitive hash
       match) and importing any that are already in a completed state.

    2. A stored hash can no longer be found in the download client (e.g. the
       torrent was removed manually, or the hash was never tracked correctly).
       Those jobs are reset to 'searching' so Prowlarr retries the search.

    3. Jobs that were marked timed_out or stalled but whose torrent actually
       finished in qBittorrent while Optimizarr was offline.  These are imported
       and their status updated to 'complete' so the history reflects the truth.

    This function runs at startup before worker threads are started so that
    imports complete before the queue resumes processing.

    Returns a summary dict with 'imported' and 'reset_to_searching' counts.
    """
    in_flight_jobs = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.status.in_([
                DownloadJobStatus.queued.value,
                DownloadJobStatus.downloading.value,
                DownloadJobStatus.moving.value,
                DownloadJobStatus.importing.value,
            ])
        )
        .all()
    )

    # All other non-imported download jobs regardless of status (except true
    # active/pending states). This allows source-name matching against qBit
    # torrents or completed-root folders even when no hash/release metadata
    # was persisted on the DownloadJob row.
    _skip_statuses = {
        DownloadJobStatus.downloading.value,  # already covered by in_flight_jobs loop
        DownloadJobStatus.queued.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.importing.value,     # already covered by in_flight_jobs loop
        DownloadJobStatus.pending.value,       # not yet started — nothing to recover
    }
    qbt_candidate_jobs = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.imported_file_path.is_(None),
            DownloadJob.status.notin_(list(_skip_statuses)),
        )
        .all()
    )
    logger.info(
        'Download startup recovery: in_flight_jobs=%d candidate_jobs=%d',
        len(in_flight_jobs),
        len(qbt_candidate_jobs),
    )

    # Arm startup grace period if any jobs are already in searching state so
    # that Prowlarr grabs don't fire the instant the monitor loop starts.
    _arm_startup_grace_if_needed(db)

    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)
    qbt_completed_root = download_client_service.get_qbt_default_save_path(qbt) if qbt.enabled else None
    linked_jobs = _link_completed_downloads_to_waiting_jobs(db)
    adopted_queue_jobs = _recover_completed_root_for_waiting_queue_jobs(db, qbt, sab, qbt_completed_root)
    adopted_queue_jobs += _recover_sab_completed_for_waiting_queue_jobs(db, qbt, sab)

    if not in_flight_jobs and not qbt_candidate_jobs and adopted_queue_jobs == 0:
        logger.info('Download startup recovery: no in-flight or unimported download jobs found')
        return {'imported': 0, 'reset_to_searching': 0, 'linked_jobs': linked_jobs, 'adopted_queue_jobs': 0}

    imported = 0
    reset_count = 0

    # Build a hash → torrent_info map covering all qBit torrents (tagged and
    # untagged) so that timed_out/stalled jobs whose tag was never applied can
    # still be matched.  Tagged torrents are preferred when there is a hash
    # conflict, so populate tagged ones first.
    qbt_map: dict[str, dict] = {}
    all_qbt_torrents: list[dict] = []
    if qbt.enabled:
        for torrent in download_client_service.get_all_qbt_tagged_torrents(qbt):
            h = torrent.get('hash', '')
            if h:
                qbt_map[h.lower()] = torrent
        # Supplement with all torrents so timed_out recovery can find
        # downloads that were never tagged (e.g. tag API failed at grab time).
        all_qbt_torrents = download_client_service.get_all_qbt_torrents(qbt)
        for torrent in all_qbt_torrents:
            h = torrent.get('hash', '').lower()
            if h and h not in qbt_map:
                qbt_map[h] = torrent

    for dj in in_flight_jobs:
        logger.info('Download startup recovery: checking job %s (hash=%s, client=%s)',
                    dj.id, dj.download_hash, dj.client_type)

        library = db.query(Library).filter(Library.id == dj.library_id).first()
        if library is None or library.profile is None:
            _mark_failed(db, dj, 'Library or profile not found during startup recovery')
            continue

        profile = library.profile

        # ── qBittorrent ──────────────────────────────────────────────────────
        if dj.client_type == 'qbittorrent':
            stored_hash = (dj.download_hash or '').lower()

            torrent_info = qbt_map.get(stored_hash)
            if torrent_info is None and stored_hash:
                # Hash might exist in qBit but wasn't tagged yet; try direct lookup
                direct = download_client_service.get_qbt_status(qbt, stored_hash)
                if direct.get('progress_percent', 0) > 0 or direct.get('is_complete'):
                    # Found it — fabricate a minimal torrent_info for the check below
                    torrent_info = {
                        'hash': stored_hash,
                        'state': 'uploading' if direct['is_complete'] else 'downloading',
                        'content_path': direct.get('save_path'),
                        'save_path': direct.get('save_path'),
                    }

            if torrent_info is None:
                recovered = _find_qbt_torrent_for_release(dj, all_qbt_torrents)
                if recovered is None and not stored_hash:
                    recovered_hash = _recover_qbt_hash_for_job(dj, all_qbt_torrents)
                    if recovered_hash:
                        recovered = next(
                            (t for t in all_qbt_torrents if str(t.get('hash', '')).lower() == recovered_hash),
                            None,
                        )
                if recovered:
                    torrent_info = recovered
                else:
                    completed_match = _find_completed_download_match(dj, qbt_completed_root)
                    if completed_match:
                        completed_name = Path(completed_match).name
                        if not _release_title_matches_profile(completed_name, profile):
                            logger.warning(
                                'Download job %s: completed offline artifact %r does not match profile; resetting to searching',
                                dj.id, completed_name,
                            )
                            _reset_download_job_to_searching(db, dj)
                            reset_count += 1
                            continue
                        logger.info(
                            'Download job %s: found completed file in qBit download root while offline; importing',
                            dj.id,
                        )
                        _import_file(db, dj, completed_match, library, profile, qbt, sab)
                        imported += 1
                        continue

                    if not stored_hash:
                        # Hash-less qBit jobs can still be reconciled in the
                        # normal monitor loop via release/source matching.
                        # Keep them downloading instead of forcing a re-search.
                        logger.warning(
                            'Download job %s: no hash yet and no qBit match during startup; '
                            'keeping status=downloading for runtime recovery',
                            dj.id,
                        )
                        dj.download_started_at = datetime.now(timezone.utc)
                        db.commit()
                        continue

                    logger.warning(
                        'Download job %s: hash %r not found in qBittorrent; resetting to searching',
                        dj.id, dj.download_hash,
                    )
                    _reset_download_job_to_searching(db, dj)
                    reset_count += 1
                    continue

            # Normalise stored hash to lowercase now that we confirmed the torrent exists
            if dj.download_hash != torrent_info.get('hash', stored_hash).lower():
                dj.download_hash = torrent_info.get('hash', stored_hash).lower()
                db.commit()

            state = torrent_info.get('state', '')
            from app.services.download_client_service import _QBT_COMPLETE_STATES
            if state in _QBT_COMPLETE_STATES:
                release_title = str(torrent_info.get('name') or dj.release_name or '')
                if not _release_title_matches_profile(release_title, profile):
                    logger.warning(
                        'Download job %s: completed torrent %r does not match profile; resetting to searching',
                        dj.id, release_title,
                    )
                    _reset_download_job_to_searching(db, dj)
                    reset_count += 1
                    continue
                save_path = torrent_info.get('content_path') or torrent_info.get('save_path')
                if save_path:
                    logger.info('Download job %s: completed while offline, importing now', dj.id)
                    _import_file(db, dj, save_path, library, profile, qbt, sab)
                    imported += 1
                else:
                    _mark_failed(db, dj, 'Torrent complete but no save path available')
            else:
                # Still downloading — reset the timeout clock to now so the job
                # gets a fresh window after the restart instead of immediately
                # timing out because created_at (or an old download_started_at)
                # is already past the threshold.
                logger.info('Download job %s: still in progress (state=%s), resuming monitoring', dj.id, state)
                dj.download_started_at = datetime.now(timezone.utc)
                db.commit()

        # ── SABnzbd ──────────────────────────────────────────────────────────
        elif dj.client_type == 'sabnzbd':
            if not dj.download_hash:
                _reset_download_job_to_searching(db, dj)
                reset_count += 1
                continue

            status = download_client_service.get_sab_status(sab, dj.download_hash)
            if status.get('is_complete') and status.get('save_path'):
                sab_release = str(dj.release_name or Path(status['save_path']).name)
                if not _release_title_matches_profile(sab_release, profile):
                    logger.warning(
                        'Download job %s: SAB completion %r does not match profile; resetting to searching',
                        dj.id, sab_release,
                    )
                    _reset_download_job_to_searching(db, dj)
                    reset_count += 1
                    continue
                logger.info('Download job %s: SABnzbd completed while offline, importing now', dj.id)
                _import_file(db, dj, status['save_path'], library, profile, qbt, sab)
                imported += 1
            elif status.get('progress_percent', 0) == 0 and not status.get('is_complete'):
                # Not found in SABnzbd at all
                logger.warning('Download job %s: NZO %r not found in SABnzbd; resetting to searching',
                               dj.id, dj.download_hash)
                _reset_download_job_to_searching(db, dj)
                reset_count += 1

        else:
            logger.warning('Download job %s: unknown client_type %r; resetting to searching',
                           dj.id, dj.client_type)
            _reset_download_job_to_searching(db, dj)
            reset_count += 1

    # ── Broad qBit recovery (all non-importing statuses) ─────────────────────
    imported += _import_completed_qbt_candidates(
        db,
        qbt,
        sab,
        qbt_completed_root,
        qbt_map,
        all_qbt_torrents,
        qbt_candidate_jobs,
        context='startup',
    )
    imported += _recover_sab_completed_for_existing_download_jobs(db, qbt, sab, context='startup')

    linked_jobs += _link_completed_downloads_to_waiting_jobs(db)

    logger.info(
        'Download startup recovery complete: imported=%s, reset_to_searching=%s, linked_jobs=%s, adopted_queue_jobs=%s',
        imported, reset_count, linked_jobs, adopted_queue_jobs,
    )
    return {
        'imported': imported,
        'reset_to_searching': reset_count,
        'linked_jobs': linked_jobs,
        'adopted_queue_jobs': adopted_queue_jobs,
    }


def _import_completed_qbt_candidates(
    db: Session,
    qbt,
    sab,
    qbt_completed_root: str | None,
    qbt_map: dict,
    all_qbt_torrents: list,
    candidate_jobs: list,
    context: str = 'recovery',
) -> int:
    """Check each candidate DownloadJob against the qBit torrent map and import
    any whose torrent has reached a completed state.

    Used by both startup recovery and post-scan recovery so the logic lives in
    one place.  Returns the number of jobs successfully imported.
    """
    from app.services.download_client_service import _QBT_COMPLETE_STATES

    imported = 0
    for dj in candidate_jobs:
        library = db.query(Library).filter(Library.id == dj.library_id).first()
        if library is None or library.profile is None:
            continue

        profile = library.profile

        if dj.client_type != 'qbittorrent' or not qbt.enabled:
            continue

        stored_hash = (dj.download_hash or '').lower()
        torrent_info = qbt_map.get(stored_hash) if stored_hash else None

        if torrent_info is None:
            torrent_info = _find_qbt_torrent_for_release(dj, all_qbt_torrents)

        if torrent_info is None:
            completed_match = _find_completed_download_match(dj, qbt_completed_root)
            if not completed_match:
                continue
            completed_name = Path(completed_match).name
            if not _release_title_matches_profile(completed_name, profile):
                logger.warning(
                    'Download %s: completed-root match %r for job %s does not match profile; skipping import',
                    context,
                    completed_name,
                    dj.id,
                )
                continue
            logger.info(
                'Download %s: job %s matched completed-root path %r; importing now',
                context,
                dj.id,
                completed_match,
            )
            dj.status = DownloadJobStatus.downloading.value
            dj.error_message = None
            db.commit()
            _import_file(db, dj, completed_match, library, profile, qbt, sab)
            imported += 1
            continue

        state = torrent_info.get('state', '')
        if state not in _QBT_COMPLETE_STATES:
            continue

        release_title = str(torrent_info.get('name') or dj.release_name or '')
        if not _release_title_matches_profile(release_title, profile):
            logger.warning(
                'Download %s: job %s completed release %r does not match profile; skipping import',
                context, dj.id, release_title,
            )
            continue

        save_path = torrent_info.get('content_path') or torrent_info.get('save_path')
        if not save_path:
            continue

        logger.info(
            'Download %s: job %s was %s but torrent completed in qBit; importing now',
            context, dj.id, dj.status,
        )
        # Reset to downloading so _import_file can transition it to complete.
        dj.status = DownloadJobStatus.downloading.value
        dj.error_message = None
        if dj.download_hash != torrent_info.get('hash', stored_hash).lower():
            dj.download_hash = torrent_info.get('hash', stored_hash).lower()
        db.commit()
        _import_file(db, dj, save_path, library, profile, qbt, sab)
        imported += 1

    return imported


def run_scan_recovery(db: Session) -> dict:
    """Check all non-active download jobs against qBittorrent after a library scan.

    Mirrors the broad-qBit check performed at startup but runs mid-runtime so
    that torrents which finished while Optimizarr was processing are imported
    before the normal queue picks up fresh work.

    New Prowlarr searches are blocked via ``_scan_recovery_event`` for the
    duration so that the download pipeline cannot advance concurrently with the
    import step.

    Returns a summary dict with an 'imported' count.
    """
    _scan_recovery_event.set()
    try:
        _skip_statuses = {
            DownloadJobStatus.pending.value,
            DownloadJobStatus.queued.value,
            DownloadJobStatus.downloading.value,
            DownloadJobStatus.moving.value,
            DownloadJobStatus.importing.value,
        }
        candidate_jobs = (
            db.query(DownloadJob)
            .filter(
                DownloadJob.imported_file_path.is_(None),
                DownloadJob.status.notin_(list(_skip_statuses)),
            )
            .all()
        )

        qbt = download_client_service.get_or_create_qbt_settings(db)
        sab = download_client_service.get_or_create_sab_settings(db)
        qbt_completed_root = download_client_service.get_qbt_default_save_path(qbt) if qbt.enabled else None

        if not candidate_jobs:
            imported = _recover_completed_root_for_waiting_queue_jobs(db, qbt, sab, qbt_completed_root)
            imported += _recover_sab_completed_for_waiting_queue_jobs(db, qbt, sab)
            imported += _recover_sab_completed_for_existing_download_jobs(db, qbt, sab, context='scan')
            logger.debug('Scan recovery: no candidate download jobs; queue adoption imported=%s', imported)
            return {'imported': imported}

        qbt_map: dict[str, dict] = {}
        all_qbt_torrents: list[dict] = []
        if qbt.enabled:
            for torrent in download_client_service.get_all_qbt_tagged_torrents(qbt):
                h = torrent.get('hash', '')
                if h:
                    qbt_map[h.lower()] = torrent
            all_qbt_torrents = download_client_service.get_all_qbt_torrents(qbt)
            for torrent in all_qbt_torrents:
                h = torrent.get('hash', '').lower()
                if h and h not in qbt_map:
                    qbt_map[h] = torrent

        imported = _import_completed_qbt_candidates(
            db,
            qbt,
            sab,
            qbt_completed_root,
            qbt_map,
            all_qbt_torrents,
            candidate_jobs,
            context='scan',
        )
        imported += _recover_completed_root_for_waiting_queue_jobs(db, qbt, sab, qbt_completed_root)
        imported += _recover_sab_completed_for_waiting_queue_jobs(db, qbt, sab)
        imported += _recover_sab_completed_for_existing_download_jobs(db, qbt, sab, context='scan')
        logger.info('Scan recovery complete: imported=%s', imported)
        return {'imported': imported}
    except Exception:
        logger.exception('Scan recovery failed')
        return {'imported': 0}
    finally:
        _scan_recovery_event.clear()
        _wake_event.set()
