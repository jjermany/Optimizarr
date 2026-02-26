from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.library import Library, LibraryProfile
from app.services import download_client_service, prowlarr_service
from app.services.job_service import create_job
from app.services.optimization_service import is_hdr_video, probe_video_height
from app.services.realtime_service import broker

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
# Signalled whenever a new DownloadJob is created so the loop wakes immediately
_wake_event = threading.Event()
_thread: threading.Thread | None = None

# Adaptive poll intervals
_POLL_DOWNLOADING_SECONDS = 3   # fast polling while files are transferring
_POLL_SEARCHING_SECONDS = 10    # moderate polling while waiting for Prowlarr
_POLL_IDLE_SECONDS = 30         # slow polling when nothing is active


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
    while not _stop_event.is_set():
        db = SessionLocal()
        sleep_seconds = _POLL_IDLE_SECONDS
        try:
            has_downloading = (
                db.query(DownloadJob)
                .filter(DownloadJob.status == DownloadJobStatus.downloading.value)
                .count()
            ) > 0
            has_searching = (
                db.query(DownloadJob)
                .filter(DownloadJob.status == DownloadJobStatus.searching.value)
                .count()
            ) > 0

            _process_searching_jobs(db)
            _process_downloading_jobs(db)

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
    """Return True if both Prowlarr and a download client are enabled."""
    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    client = download_client_service.get_or_create_settings(db)
    return bool(prowlarr.enabled and client.enabled)


def download_job_exists_for_source(db: Session, source_path: str) -> bool:
    active_statuses = {
        DownloadJobStatus.searching.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
        DownloadJobStatus.complete.value,
    }
    return db.query(
        db.query(DownloadJob)
        .filter(
            DownloadJob.source_file_path == source_path,
            DownloadJob.status.in_(active_statuses),
        )
        .exists()
    ).scalar()


def create_download_job(db: Session, source_path: str, library: Library, profile: LibraryProfile) -> DownloadJob:
    dj = DownloadJob(
        library_id=library.id,
        source_file_path=source_path,
        status=DownloadJobStatus.searching.value,
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
    return {
        'id': dj.id,
        'library_id': dj.library_id,
        'source_file_path': dj.source_file_path,
        'search_query': dj.search_query,
        'download_hash': dj.download_hash,
        'status': dj.status,
        'progress_percent': dj.progress_percent,
        'downloaded_file_path': dj.downloaded_file_path,
        'imported_file_path': dj.imported_file_path,
        'error_message': dj.error_message,
        'encode_job_id': dj.encode_job_id,
        'created_at': dj.created_at.isoformat() if dj.created_at else None,
        'completed_at': dj.completed_at.isoformat() if dj.completed_at else None,
    }


def _publish_download_job(dj: DownloadJob) -> None:
    broker.publish('download_job_update', download_job_to_dict(dj))


# ─────────────────────────────────────────────────────────────────────────────
# Search query construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_search_query(source_path: str, profile: LibraryProfile) -> str:
    stem = Path(source_path).stem
    # Normalize dots/underscores to spaces
    clean = re.sub(r'[._]', ' ', stem)
    # Extract 4-digit year
    year_match = re.search(r'\b(19|20)\d{2}\b', clean)
    if year_match:
        year = year_match.group(0)
        title = clean[:year_match.start()].strip()
    else:
        year = ''
        title = clean.strip()
    resolution = f"{profile.target_resolution}p"
    return ' '.join(filter(None, [title, year, resolution]))


def _is_hdr_release(title_lower: str) -> bool:
    """Return True if the release title indicates HDR content."""
    # 'hdr' catches hdr, hdr10, hdr10+; 'dolby vision' catches full name
    if any(tag in title_lower for tag in ('hdr', 'dolby vision')):
        return True
    # Word-boundary match for standalone 'dv' or 'hlg' to avoid false positives
    return bool(re.search(r'\b(dv|hlg)\b', title_lower))


def _rank_candidates(releases: list[dict]) -> list[dict]:
    """Sort releases by seeders descending, then size ascending."""
    return sorted(releases, key=lambda r: (-r.get('seeders', 0), r.get('size', 0)))


def _select_best_release(releases: list[dict], profile: LibraryProfile) -> dict | None:
    """
    Two-pass selection:
    1. Always prefer an SDR release at the target resolution.
    2. If no SDR release exists and tone_map_hdr is enabled, accept an HDR
       release (it will be tone-mapped via an encode job after import).
    3. Return None if nothing suitable is found → caller falls back to encoding.
    """
    res_str = f"{profile.target_resolution}p"
    sdr_candidates: list[dict] = []
    hdr_candidates: list[dict] = []

    for r in releases:
        title_lower = r.get('title', '').lower()
        # Must mention the target resolution
        if res_str.lower() not in title_lower:
            continue
        if _is_hdr_release(title_lower):
            hdr_candidates.append(r)
        else:
            sdr_candidates.append(r)

    # Pass 1: SDR — always preferred regardless of tone_map_hdr setting
    if sdr_candidates:
        return _rank_candidates(sdr_candidates)[0]

    # Pass 2: HDR — only acceptable when tone-mapping is enabled
    if getattr(profile, 'tone_map_hdr', False) and hdr_candidates:
        logger.info(
            'No SDR release found; accepting HDR release (will be tone-mapped after import)'
        )
        return _rank_candidates(hdr_candidates)[0]

    # Nothing suitable found; caller will fall back to encoding the original
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Processing: searching → downloading
# ─────────────────────────────────────────────────────────────────────────────

def _process_searching_jobs(db: Session) -> None:
    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    client_settings = download_client_service.get_or_create_settings(db)

    if not prowlarr.enabled or not client_settings.enabled:
        return

    jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.searching.value)
        .all()
    )

    for dj in jobs:
        _do_search(db, dj, prowlarr, client_settings)


def _do_search(db: Session, dj: DownloadJob, prowlarr, client_settings) -> None:
    library = db.query(Library).filter(Library.id == dj.library_id).first()
    if library is None or library.profile is None:
        _mark_failed(db, dj, 'Library or profile not found')
        return

    profile = library.profile
    query = _build_search_query(dj.source_file_path, profile)
    dj.search_query = query
    db.commit()

    logger.info('Download job %s searching Prowlarr for %r', dj.id, query)
    releases = prowlarr_service.search(prowlarr, query)
    best = _select_best_release(releases, profile)

    if best is None:
        logger.info('Download job %s: no matching release found, falling back to encode', dj.id)
        _mark_failed(db, dj, 'No matching release found')
        _fallback_to_encode(db, dj, library, profile)
        return

    logger.info('Download job %s: grabbing release %r', dj.id, best.get('title'))
    grab_result = prowlarr_service.grab(prowlarr, best.get('guid', ''), best.get('indexerId', 0))

    if grab_result is None:
        _mark_failed(db, dj, 'Prowlarr grab failed')
        _fallback_to_encode(db, dj, library, profile)
        return

    # Extract hash/id from grab result. Prowlarr may return downloadClientId or similar.
    download_hash = (
        grab_result.get('downloadId')
        or grab_result.get('downloadClientId')
        or grab_result.get('hash')
        or grab_result.get('id')
        or ''
    )
    if not download_hash:
        logger.warning('Download job %s: grab succeeded but no hash returned; result: %s', dj.id, json.dumps(grab_result)[:200])
        _mark_failed(db, dj, 'No download ID returned from Prowlarr grab')
        _fallback_to_encode(db, dj, library, profile)
        return

    dj.download_hash = str(download_hash)
    dj.status = DownloadJobStatus.downloading.value
    dj.progress_percent = 0
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    logger.info('Download job %s now downloading; hash=%s', dj.id, dj.download_hash)


# ─────────────────────────────────────────────────────────────────────────────
# Processing: downloading → complete/failed/timed_out
# ─────────────────────────────────────────────────────────────────────────────

def _process_downloading_jobs(db: Session) -> None:
    client_settings = download_client_service.get_or_create_settings(db)
    if not client_settings.enabled:
        return

    jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.downloading.value)
        .all()
    )

    for dj in jobs:
        _check_download_progress(db, dj, client_settings)


def _check_download_progress(db: Session, dj: DownloadJob, client_settings) -> None:
    library = db.query(Library).filter(Library.id == dj.library_id).first()
    if library is None or library.profile is None:
        _mark_failed(db, dj, 'Library or profile not found')
        return

    profile = library.profile
    timeout_minutes = int(getattr(profile, 'download_timeout_minutes', 60) or 60)
    elapsed = datetime.utcnow() - dj.created_at
    if elapsed > timedelta(minutes=timeout_minutes):
        logger.warning('Download job %s timed out after %s minutes', dj.id, timeout_minutes)
        dj.status = DownloadJobStatus.timed_out.value
        dj.error_message = f'Download timed out after {timeout_minutes} minutes'
        dj.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        _fallback_to_encode(db, dj, library, profile)
        return

    status = download_client_service.get_download_status(client_settings, dj.download_hash or '')
    progress = status.get('progress_percent', 0)

    if progress != dj.progress_percent:
        dj.progress_percent = progress
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)

    if status.get('is_complete'):
        save_path = status.get('save_path')
        if save_path:
            logger.info('Download job %s complete; save_path=%r', dj.id, save_path)
            _import_file(db, dj, save_path, library, profile)
        else:
            _mark_failed(db, dj, 'Download marked complete but no save path returned')
            _fallback_to_encode(db, dj, library, profile)
    elif status.get('is_stalled'):
        logger.warning('Download job %s is stalled', dj.id)
        dj.status = DownloadJobStatus.stalled.value
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        # Stalled counts as timed-out for fallback purposes
        _fallback_to_encode(db, dj, library, profile)


# ─────────────────────────────────────────────────────────────────────────────
# Import: move downloaded file to library
# ─────────────────────────────────────────────────────────────────────────────

def _import_file(db: Session, dj: DownloadJob, save_path: str, library: Library, profile: LibraryProfile) -> None:
    dj.status = DownloadJobStatus.importing.value
    db.commit()
    _publish_download_job(dj)

    video_file = download_client_service.find_video_in_path(save_path)
    if video_file is None:
        logger.error('Download job %s: no video file found in %r', dj.id, save_path)
        _mark_failed(db, dj, f'No video file found in {save_path}')
        _fallback_to_encode(db, dj, library, profile)
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
            dj.completed_at = datetime.utcnow()
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
            return

    # Move file to destination
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if _is_same_filesystem(video_file, dest.parent):
            os.replace(video_file, dest)
        else:
            temp_dest = dest.parent / f'.optimizarr-import-{dj.id}-{int(time.time() * 1000)}{dest.suffix}'
            shutil.copy2(video_file, temp_dest)
            os.replace(temp_dest, dest)
            video_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.error('Download job %s: failed to move %r → %r: %s', dj.id, video_file, dest, exc)
        _mark_failed(db, dj, f'File move failed: {exc}')
        _fallback_to_encode(db, dj, library, profile)
        return

    logger.info('Download job %s: imported %r → %r', dj.id, str(video_file), str(dest))
    dj.imported_file_path = str(dest)

    # Post-import validation: check if HDR tone-mapping is needed
    if getattr(profile, 'tone_map_hdr', False) and is_hdr_video(str(dest)):
        logger.info('Download job %s: imported file is HDR and tone_map_hdr=True; queuing encode', dj.id)
        try:
            height = probe_video_height(str(dest))
            hdr = True
            encode_job = create_job(
                db,
                str(dest),
                library_id=library.id,
                profile=profile,
                source_resolution=height,
                source_is_hdr=hdr,
            )
            dj.encode_job_id = encode_job.id
        except Exception as exc:
            logger.warning('Download job %s: failed to create tone-map encode job: %s', dj.id, exc)

    dj.status = DownloadJobStatus.complete.value
    dj.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)

    # Notify Plex if configured
    try:
        from app.services.plex_service import trigger_scan_after_job
        trigger_scan_after_job(library.id)
    except Exception:
        pass


def _is_same_filesystem(file_path: Path, target_dir: Path) -> bool:
    try:
        return os.stat(file_path).st_dev == os.stat(target_dir).st_dev
    except OSError:
        return False


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
        if dj.status not in (DownloadJobStatus.timed_out.value, DownloadJobStatus.failed.value, DownloadJobStatus.stalled.value):
            dj.status = DownloadJobStatus.fallback_queued.value
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        broker.publish_notification(
            f'Download failed for {Path(dj.source_file_path).name}; falling back to encode'
        )
        logger.info('Download job %s: fallback encode job %s created', dj.id, encode_job.id)
    except Exception as exc:
        logger.error('Download job %s: failed to create fallback encode job: %s', dj.id, exc)


def _mark_failed(db: Session, dj: DownloadJob, reason: str) -> None:
    dj.status = DownloadJobStatus.failed.value
    dj.error_message = reason
    dj.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    logger.warning('Download job %s failed: %s', dj.id, reason)


