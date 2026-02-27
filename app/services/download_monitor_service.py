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
from app.models.library import DownloadQualityProfileEnum, Library, LibraryProfile
from app.services import download_client_service, prowlarr_service
from app.services.job_service import create_job
from app.services.optimization_service import is_hdr_video, probe_video_height
from app.services.realtime_service import broker

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
# Signalled whenever a new DownloadJob is created so the loop wakes immediately
_wake_event = threading.Event()
_thread: threading.Thread | None = None

# When set, the search pipeline is halted after an import error so the user
# can investigate before more downloads are started.
_download_queue_stopped: bool = False
_download_queue_stop_reason: str = ''


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
    """Return True if Prowlarr is enabled and at least one download client is enabled."""
    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    if not prowlarr.enabled:
        return False
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)
    return bool(qbt.enabled or sab.enabled)


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
        'client_type': dj.client_type,
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
# Quality profile helpers
# ─────────────────────────────────────────────────────────────────────────────

# Keywords to append to the Prowlarr search query for each quality profile
_QUALITY_SEARCH_TERMS: dict[str, str] = {
    DownloadQualityProfileEnum.remux.value:  'REMUX',
    DownloadQualityProfileEnum.web_dl.value: 'WEB-DL',
    DownloadQualityProfileEnum.webrip.value: 'WEBRip',
    DownloadQualityProfileEnum.bluray.value: 'BluRay',
    DownloadQualityProfileEnum.hdtv.value:   'HDTV',
}

# Substrings to look for in a release title to confirm its quality source.
# Multiple aliases cover common scene/release group naming conventions.
_QUALITY_TITLE_KEYWORDS: dict[str, list[str]] = {
    DownloadQualityProfileEnum.remux.value:  ['remux'],
    DownloadQualityProfileEnum.web_dl.value: ['web-dl', 'webdl', 'web dl'],
    DownloadQualityProfileEnum.webrip.value: ['webrip', 'web-rip'],
    DownloadQualityProfileEnum.bluray.value: ['bluray', 'blu-ray', 'bdrip', 'bluray'],
    DownloadQualityProfileEnum.hdtv.value:   ['hdtv'],
}


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
    quality_val = str(getattr(profile, 'download_quality_profile', DownloadQualityProfileEnum.any) or DownloadQualityProfileEnum.any)
    quality_term = _QUALITY_SEARCH_TERMS.get(quality_val, '')
    return ' '.join(filter(None, [title, year, resolution, quality_term]))


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


def _select_best_release(
    releases: list[dict],
    profile: LibraryProfile,
    qbt_enabled: bool,
    sab_enabled: bool,
) -> dict | None:
    """
    Select the best SDR release at the target resolution, respecting the
    configured quality profile filter and which download clients are enabled.

    HDR releases are always rejected.  If a specific quality profile is set
    only releases whose title contains a matching keyword are accepted.
    Torrent releases require qBittorrent to be enabled; usenet releases
    require SABnzbd to be enabled.
    """
    res_str = f"{profile.target_resolution}p"
    quality_val = str(getattr(profile, 'download_quality_profile', DownloadQualityProfileEnum.any) or DownloadQualityProfileEnum.any)
    quality_keywords = _QUALITY_TITLE_KEYWORDS.get(quality_val, [])

    sdr_candidates: list[dict] = []

    for r in releases:
        title_lower = r.get('title', '').lower()

        # Resolution filter
        if res_str.lower() not in title_lower:
            continue

        # HDR filter — never accept HDR downloads
        if _is_hdr_release(title_lower):
            continue

        # Quality source filter
        if quality_keywords and not any(kw in title_lower for kw in quality_keywords):
            continue

        # Protocol / client availability filter
        protocol = r.get('protocol', '').lower()
        if protocol == 'torrent' and not qbt_enabled:
            continue
        if protocol == 'usenet' and not sab_enabled:
            continue

        sdr_candidates.append(r)

    if sdr_candidates:
        return _rank_candidates(sdr_candidates)[0]

    return None  # no matching release found; caller falls back to encoding


def _client_type_for_protocol(protocol: str) -> str:
    """Map Prowlarr release protocol to our client_type string."""
    if protocol.lower() == 'torrent':
        return 'qbittorrent'
    return 'sabnzbd'


# ─────────────────────────────────────────────────────────────────────────────
# Processing: searching → downloading
# ─────────────────────────────────────────────────────────────────────────────

def _process_searching_jobs(db: Session) -> None:
    if _download_queue_stopped:
        return

    prowlarr = prowlarr_service.get_or_create_prowlarr_settings(db)
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)

    if not prowlarr.enabled or (not qbt.enabled and not sab.enabled):
        return

    # Serial pipeline: don't start a new search while a download or import is
    # already in progress.  This ensures items are fully processed one at a time:
    # search → download → import → complete → next item.
    active = (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_([
            DownloadJobStatus.downloading.value,
            DownloadJobStatus.importing.value,
        ]))
        .count()
    )
    if active > 0:
        return

    # Pick the oldest pending search so the queue is processed in order.
    dj = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.searching.value)
        .order_by(DownloadJob.created_at.asc())
        .first()
    )
    if dj:
        _do_search(db, dj, prowlarr, qbt, sab)


def _do_search(db: Session, dj: DownloadJob, prowlarr, qbt, sab) -> None:
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
    best = _select_best_release(releases, profile, qbt_enabled=qbt.enabled, sab_enabled=sab.enabled)

    if best is None:
        logger.info('Download job %s: no matching release found, falling back to encode', dj.id)
        _mark_failed(db, dj, 'No matching release found')
        _fallback_to_encode(db, dj, library, profile)
        return

    # Safety check: refuse to grab if another download is already active.
    # Guards against any state inconsistency that slips past the outer check.
    if db.query(DownloadJob).filter(DownloadJob.status.in_([
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.importing.value,
    ])).count() > 0:
        logger.warning('Download job %s: active download detected before grab; deferring', dj.id)
        return

    logger.info('Download job %s: grabbing release %r', dj.id, best.get('title'))
    grab_result = prowlarr_service.grab(prowlarr, best.get('guid', ''), best.get('indexerId', 0))

    if grab_result is None:
        _mark_failed(db, dj, 'Prowlarr grab failed')
        _fallback_to_encode(db, dj, library, profile)
        return

    download_hash = (
        grab_result.get('downloadId')
        or grab_result.get('downloadClientId')
        or grab_result.get('hash')
        or grab_result.get('id')
        or ''
    )
    if not download_hash:
        # Prowlarr's HTTP call succeeded (torrent IS in qBit) but no hash was
        # returned.  Do NOT fall back to encode — that would unblock the serial
        # constraint and let a second torrent be sent while the first sits
        # untracked.  Instead keep status=downloading so the queue stays blocked;
        # _check_download_progress() will scan qBit to recover the hash.
        logger.warning(
            'Download job %s: grab succeeded but no hash returned; '
            'will attempt qBit recovery scan. result=%s',
            dj.id, json.dumps(grab_result)[:200],
        )
        client_type = _client_type_for_protocol(best.get('protocol', ''))
        dj.download_hash = None
        dj.client_type = client_type
        dj.status = DownloadJobStatus.downloading.value
        dj.progress_percent = 0
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        return

    # Determine which client received this download based on the release protocol
    client_type = _client_type_for_protocol(best.get('protocol', ''))

    # qBittorrent stores hashes in lowercase; normalise so lookups always match.
    normalised_hash = str(download_hash).lower() if client_type == 'qbittorrent' else str(download_hash)
    dj.download_hash = normalised_hash
    dj.client_type = client_type
    dj.status = DownloadJobStatus.downloading.value
    dj.progress_percent = 0
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    logger.info('Download job %s now downloading via %s; hash=%s', dj.id, client_type, dj.download_hash)

    # Tag the torrent in qBittorrent so it's identifiable in the client UI
    if client_type == 'qbittorrent' and qbt.enabled:
        download_client_service.tag_qbt_torrent(qbt, str(download_hash))


# ─────────────────────────────────────────────────────────────────────────────
# Processing: downloading → complete/failed/timed_out
# ─────────────────────────────────────────────────────────────────────────────

def _process_downloading_jobs(db: Session) -> None:
    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)

    jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.downloading.value)
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

    client_type = dj.client_type or 'qbittorrent'

    # If the hash is unknown (grab succeeded but Prowlarr returned no hash),
    # scan all qBit torrents for one added since this job was created.
    # Since only one Optimizarr download runs at a time, any new torrent is ours.
    if not dj.download_hash and client_type == 'qbittorrent' and qbt.enabled:
        job_ts = dj.created_at.timestamp()
        recent = [
            t for t in download_client_service.get_all_qbt_torrents(qbt)
            if t.get('added_on', 0) >= job_ts
        ]
        if len(recent) == 1:
            recovered_hash = recent[0]['hash'].lower()
            logger.info('Download job %s: recovered hash %s via qBit scan', dj.id, recovered_hash)
            dj.download_hash = recovered_hash
            db.commit()
            db.refresh(dj)
            download_client_service.tag_qbt_torrent(qbt, recovered_hash)
        elif not recent:
            logger.debug('Download job %s: no recent qBit torrent found yet; waiting', dj.id)
            return
        else:
            logger.warning(
                'Download job %s: %d recent qBit torrents found; cannot recover hash unambiguously — waiting',
                dj.id, len(recent),
            )
            return

    status = download_client_service.get_download_status(client_type, qbt, sab, dj.download_hash or '')
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
            _import_file(db, dj, save_path, library, profile, qbt, sab)
        else:
            _mark_failed(db, dj, 'Download marked complete but no save path returned')
            _fallback_to_encode(db, dj, library, profile)
    elif status.get('is_stalled'):
        logger.warning('Download job %s is stalled', dj.id)
        dj.status = DownloadJobStatus.stalled.value
        db.commit()
        db.refresh(dj)
        _publish_download_job(dj)
        _fallback_to_encode(db, dj, library, profile)


# ─────────────────────────────────────────────────────────────────────────────
# Import: move downloaded file from complete dir to library
# ─────────────────────────────────────────────────────────────────────────────

def _import_file(db: Session, dj: DownloadJob, save_path: str, library: Library, profile: LibraryProfile, qbt, sab) -> None:
    """
    Import a completed download into the library.

    The download client moves files from its incomplete directory to its
    complete directory automatically.  Optimizarr only needs access to the
    complete directory; it reads the video file from there and moves/copies
    it to the library's media folder.
    """
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
            _cleanup_download_client(dj, qbt, sab)
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
                os.replace(video_file, dest)
            else:
                temp_dest = dest.parent / f'.optimizarr-import-{dj.id}-{int(time.time() * 1000)}{dest.suffix}'
                shutil.copy2(video_file, temp_dest)
                os.replace(temp_dest, dest)
                video_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.error('Download job %s: failed to import %r → %r: %s', dj.id, video_file, dest, exc)
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
    dj.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)

    # Clean up the download client entry after a successful import
    _cleanup_download_client(dj, qbt, sab)

    # Notify Plex if configured
    try:
        from app.services.plex_service import trigger_scan_after_job
        trigger_scan_after_job(library.id)
    except Exception:
        pass

    # Wake the monitor immediately so the next queued search can begin without
    # waiting for the full idle poll interval.
    _wake_event.set()


def _cleanup_download_client(dj: DownloadJob, qbt, sab) -> None:
    """
    Post-import client cleanup:
    - SABnzbd: remove the history entry (files are already in the library).
    - qBittorrent: leave untouched so it can follow its own seeding rules.
    """
    if dj.client_type == 'sabnzbd' and sab is not None and dj.download_hash:
        download_client_service.delete_sab_history(sab, dj.download_hash)


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


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery
# ─────────────────────────────────────────────────────────────────────────────

def run_download_startup_recovery(db: Session) -> dict:
    """Reconcile in-flight download jobs against the download client on startup.

    Handles two scenarios that occur when Optimizarr restarts:

    1. A torrent finished downloading while the app was offline — qBittorrent
       has the file ready but we never ran the import step.  We detect this by
       scanning all 'optimizarr'-tagged torrents in qBit (case-insensitive hash
       match) and importing any that are already in a completed state.

    2. A stored hash can no longer be found in the download client (e.g. the
       torrent was removed manually, or the hash was never tracked correctly).
       Those jobs are reset to 'searching' so Prowlarr retries the search.

    Returns a summary dict with 'imported' and 'reset_to_searching' counts.
    """
    downloading_jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == DownloadJobStatus.downloading.value)
        .all()
    )
    if not downloading_jobs:
        logger.info('Download startup recovery: no in-flight download jobs found')
        return {'imported': 0, 'reset_to_searching': 0}

    qbt = download_client_service.get_or_create_qbt_settings(db)
    sab = download_client_service.get_or_create_sab_settings(db)

    imported = 0
    reset_count = 0

    # Build a hash → torrent_info map from all tagged qBit torrents so we can
    # find completed downloads even when the stored hash has wrong casing.
    qbt_map: dict[str, dict] = {}
    if qbt.enabled:
        for torrent in download_client_service.get_all_qbt_tagged_torrents(qbt):
            h = torrent.get('hash', '')
            if h:
                qbt_map[h.lower()] = torrent

    for dj in downloading_jobs:
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
                logger.warning('Download job %s: hash %r not found in qBittorrent; resetting to searching',
                               dj.id, dj.download_hash)
                dj.status = DownloadJobStatus.searching.value
                dj.download_hash = None
                dj.client_type = None
                dj.progress_percent = 0
                db.commit()
                db.refresh(dj)
                _publish_download_job(dj)
                reset_count += 1
                continue

            # Normalise stored hash to lowercase now that we confirmed the torrent exists
            if dj.download_hash != torrent_info.get('hash', stored_hash).lower():
                dj.download_hash = torrent_info.get('hash', stored_hash).lower()
                db.commit()

            state = torrent_info.get('state', '')
            from app.services.download_client_service import _QBT_COMPLETE_STATES
            if state in _QBT_COMPLETE_STATES:
                save_path = torrent_info.get('content_path') or torrent_info.get('save_path')
                if save_path:
                    logger.info('Download job %s: completed while offline, importing now', dj.id)
                    _import_file(db, dj, save_path, library, profile, qbt, sab)
                    imported += 1
                else:
                    _mark_failed(db, dj, 'Torrent complete but no save path available')
            else:
                # Still downloading — leave as-is; the normal loop will catch up
                logger.info('Download job %s: still in progress (state=%s), resuming monitoring', dj.id, state)

        # ── SABnzbd ──────────────────────────────────────────────────────────
        elif dj.client_type == 'sabnzbd':
            if not dj.download_hash:
                dj.status = DownloadJobStatus.searching.value
                dj.progress_percent = 0
                db.commit()
                db.refresh(dj)
                _publish_download_job(dj)
                reset_count += 1
                continue

            status = download_client_service.get_sab_status(sab, dj.download_hash)
            if status.get('is_complete') and status.get('save_path'):
                logger.info('Download job %s: SABnzbd completed while offline, importing now', dj.id)
                _import_file(db, dj, status['save_path'], library, profile, qbt, sab)
                imported += 1
            elif status.get('progress_percent', 0) == 0 and not status.get('is_complete'):
                # Not found in SABnzbd at all
                logger.warning('Download job %s: NZO %r not found in SABnzbd; resetting to searching',
                               dj.id, dj.download_hash)
                dj.status = DownloadJobStatus.searching.value
                dj.download_hash = None
                dj.client_type = None
                dj.progress_percent = 0
                db.commit()
                db.refresh(dj)
                _publish_download_job(dj)
                reset_count += 1

        else:
            logger.warning('Download job %s: unknown client_type %r; resetting to searching',
                           dj.id, dj.client_type)
            dj.status = DownloadJobStatus.searching.value
            dj.download_hash = None
            dj.client_type = None
            dj.progress_percent = 0
            db.commit()
            db.refresh(dj)
            _publish_download_job(dj)
            reset_count += 1

    logger.info('Download startup recovery complete: imported=%s, reset_to_searching=%s',
                imported, reset_count)
    return {'imported': imported, 'reset_to_searching': reset_count}
