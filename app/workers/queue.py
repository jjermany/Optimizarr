from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import shutil
from threading import Event, Lock, Thread
import time

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import coerce_workspace_root
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job
from app.models.library import Library, LibraryProfile, SchedulePolicyEnum
from app.models.settings import QueueSortEnum, Settings
from app.services.job_service import _probe_partial_duration, prune_job_history
from app.services.notification_service import enqueue_job_complete, enqueue_job_failed, enqueue_low_disk_space_alert, format_display_name, handle_job_terminal_state
from app.services.job_timing_service import start_encode_timing, stop_encode_timing, touch_encode_timing
from app.services.plex_service import trigger_scan_after_job
from app.services.optimization_service import (
    delete_partial_output,
    get_active_position,
    is_hdr_video,
    optimize_video,
    probe_video_height,
    stop_active_ffmpeg,
)
from app.services.realtime_service import broker

stop_event = Event()
_pool_lock = Lock()
_active_workers: dict[int, Thread] = {}
_manager_thread: Thread | None = None
_workers_allowed = True
_queue_paused = False
_last_prune_at = 0.0
_last_schedule_check_at = 0.0
_last_workers_allowed: bool | None = None

logger = logging.getLogger(__name__)


TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}
PAUSED_STATUSES = {'paused', 'paused_schedule'}
MIN_SOURCE_HEIGHT = 2000
_disk_space_alert_active = False

BYTES_PER_GB = 1024 * 1024 * 1024


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _should_cancel(db: Session, job_id: int) -> bool:
    if stop_event.is_set():
        return True
    job = db.query(Job).filter(Job.id == job_id).first()
    return bool(job and job.cancel_requested)


def _mark_finished(job: Job) -> None:
    if job.status in TERMINAL_STATUSES:
        stop_encode_timing(job)
        job.completed_at = datetime.now(UTC)


def _publish_job(job: Job, *, throttle_progress: bool = True) -> None:
    broker.publish_job_update(
        {
            'id': job.id,
            'status': job.status,
            'source_path': job.source_path,
            'output_path': job.output_path,
            'retry_count': job.retry_count,
            'cancel_requested': job.cancel_requested,
            'progress_percent': job.progress_percent,
            'fps': job.fps,
            'eta_seconds': job.eta_seconds,
            'encoder_used': job.encoder_used,
            'codec_used': job.codec_used,
            'hwaccel_used': job.hwaccel_used,
            'used_fallback': job.used_fallback,
            'fallback_reason': job.fallback_reason,
            'error_message': job.error_message,
            'source_resolution': job.source_resolution,
            'source_is_hdr': job.source_is_hdr,
            'encode_duration_seconds': job.encode_duration_seconds,
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        },
        throttle_progress=throttle_progress,
    )


def _profile_snapshot(job: Job) -> dict:
    if not job.profile_snapshot_json:
        return {}
    try:
        parsed = json.loads(job.profile_snapshot_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _preflight_output_path(job: Job, snapshot: dict) -> str:
    source = Path(job.input_path)
    output_suffix = str(snapshot.get('output_suffix') or '-1080p')
    container = str(snapshot.get('container') or '').strip().lstrip('.')
    extension = container or source.suffix.lstrip('.') or 'mkv'
    return str(source.with_name(f'{source.stem}{output_suffix}.{extension}'))


def _cache_free_bytes(settings: Settings) -> int:
    workspace_root = str(getattr(settings, 'workspace_root', '') or '').strip()
    workspace_path = Path(coerce_workspace_root(workspace_root)) if workspace_root else None

    probe_path = workspace_path
    while probe_path and not probe_path.exists():
        parent = probe_path.parent
        if parent == probe_path:
            probe_path = None
            break
        probe_path = parent

    target_path = probe_path if probe_path else Path('/')
    return shutil.disk_usage(target_path).free


def _required_cache_free_bytes(settings: Settings) -> int:
    min_free_gb = max(1, int(getattr(settings, 'min_free_gb', 25) or 25))
    return min_free_gb * BYTES_PER_GB


def _pause_queue_for_low_disk_space(settings: Settings, free_bytes: int, job: Job | None = None) -> None:
    global _disk_space_alert_active
    pause_queue(reason='low_disk')

    with _pool_lock:
        if _disk_space_alert_active:
            return
        _disk_space_alert_active = True

    min_free_gb = max(1, int(getattr(settings, 'min_free_gb', 25) or 25))
    free_gb = free_bytes / BYTES_PER_GB
    logger.warning('Queue paused due to low cache space: %.2f GB free < %s GB required', free_gb, min_free_gb)
    library_name = None
    file_name = None
    if job:
        file_name = format_display_name(job.source_path)

    enqueue_low_disk_space_alert(
        min_free_gb=min_free_gb,
        free_gb=free_gb,
        library_name=library_name,
        file_name=file_name,
    )
    broker.publish_notification('queue_paused_low_disk')


def _clear_low_disk_alert() -> None:
    global _disk_space_alert_active
    with _pool_lock:
        _disk_space_alert_active = False


def _apply_output_conflict_policy(job: Job, snapshot: dict) -> bool:
    policy = str(snapshot.get('output_conflict_policy') or 'skip').lower()
    output_path = Path(job.output_path or '')
    if not output_path.exists():
        return True

    if policy == 'overwrite':
        output_path.unlink(missing_ok=True)
        return True

    if policy == 'rename':
        candidate = output_path
        version = 2
        while candidate.exists():
            candidate = output_path.with_name(f'{output_path.stem}-v{version}{output_path.suffix}')
            version += 1
        job.output_path = str(candidate)
        snapshot['resolved_output_path'] = job.output_path
        job.profile_snapshot_json = json.dumps(snapshot)
        return True

    job.status = 'skipped'
    job.error_message = 'Output exists'
    return False


def preflight_job(job: Job, settings: Settings) -> bool:
    snapshot = _profile_snapshot(job)

    if not Path(job.input_path).exists():
        job.status = 'skipped'
        job.error_message = 'Input missing'
        return False

    height = probe_video_height(job.input_path)
    if height is None:
        job.status = 'failed'
        job.error_message = 'Unable to probe input'
        return False

    hdr_only = bool(snapshot.get('hdr_only'))
    tone_map_hdr = bool(snapshot.get('tone_map_hdr'))
    source_is_hdr = is_hdr_video(job.input_path) if (hdr_only or tone_map_hdr) else False

    if hdr_only and not source_is_hdr:
        job.status = 'skipped'
        job.error_message = 'No longer matches criteria'
        return False

    minimum_source_resolution = int(snapshot.get('minimum_source_resolution') or MIN_SOURCE_HEIGHT)
    if height < minimum_source_resolution:
        job.status = 'skipped'
        job.error_message = 'No longer matches criteria'
        return False

    # Tone-map jobs should still run at or below target resolution because the
    # HDR->SDR conversion itself is the required processing.
    skip_target_check = tone_map_hdr and source_is_hdr
    target_resolution = snapshot.get('target_resolution')
    if not skip_target_check and isinstance(target_resolution, int) and height <= target_resolution:
        job.status = 'skipped'
        job.error_message = 'No longer matches criteria'
        return False

    job.output_path = _preflight_output_path(job, snapshot)
    if not _apply_output_conflict_policy(job, snapshot):
        return False

    free_bytes = _cache_free_bytes(settings)
    if free_bytes < _required_cache_free_bytes(settings):
        _pause_queue_for_low_disk_space(settings, free_bytes, job)
        job.status = 'failed'
        job.error_message = 'Insufficient cache space'
        return False

    _clear_low_disk_alert()

    return True


def _process_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        if job.cancel_requested:
            job.status = 'cancelled'
            _mark_finished(job)
            db.commit()
            _publish_job(job, throttle_progress=False)
            return

        settings = _get_settings(db)

        # Capture any saved resume position before the preflight resets job fields.
        resume_position = job.resume_position_seconds

        job.status = 'preflight'
        # Preserve existing progress when resuming so the UI shows the already-done portion.
        if not resume_position:
            job.progress_percent = 0
        job.error_message = None
        job.encoder_used = None
        job.codec_used = None
        job.hwaccel_used = None
        job.used_fallback = None
        job.fallback_reason = None
        db.commit()
        _publish_job(job, throttle_progress=False)

        if not preflight_job(job, settings):
            _mark_finished(job)
            db.commit()
            _publish_job(job, throttle_progress=False)
            if job.status == 'failed':
                enqueue_job_failed(job)
            handle_job_terminal_state(job.id, job.status)
            return

        # Clear the resume position from the DB now that we are about to use it;
        # on success it is no longer needed, and on failure the job will be retried
        # from scratch or re-paused with a new position.
        job.resume_position_seconds = None
        job.status = 'running'
        start_encode_timing(job)
        db.commit()
        _publish_job(job, throttle_progress=False)

        def on_encoder_selected(encoder_name: str, hwaccel: bool) -> None:
            db.refresh(job)
            if job.status in TERMINAL_STATUSES:
                return
            job.encoder_used = encoder_name
            job.hwaccel_used = hwaccel
            touch_encode_timing(job)
            db.commit()
            _publish_job(job, throttle_progress=False)

        def on_progress(update: dict[str, float | int | None]) -> None:
            db.refresh(job)
            if job.status in TERMINAL_STATUSES or job.status in PAUSED_STATUSES:
                return
            job.progress_percent = int(update.get('progress_percent') or 0)
            fps_val = update.get('fps')
            job.fps = float(fps_val) if isinstance(fps_val, (int, float)) else None
            eta_val = update.get('eta_seconds')
            job.eta_seconds = int(eta_val) if isinstance(eta_val, (int, float)) else None
            touch_encode_timing(job)
            db.commit()
            _publish_job(job, throttle_progress=True)

        settings.profile_snapshot_json = job.profile_snapshot_json
        metrics = optimize_video(
            job.input_path,
            settings,
            job_id=job.id,
            progress_callback=on_progress,
            should_cancel=lambda: _should_cancel(db, job_id),
            encoder_selected_callback=on_encoder_selected,
            resume_position_seconds=resume_position,
        )

        db.refresh(job)
        if job.status in {'paused', 'paused_schedule'}:
            db.commit()
            _publish_job(job, throttle_progress=False)
            return
        if job.status == 'queued':
            # A manual action re-queued this item while the current worker was
            # still winding down after FFmpeg stop (for example "restart from
            # beginning" or cancel-to-queue). Preserve that newer queue state
            # instead of letting the stale worker overwrite it as cancelled.
            job.fps = None
            job.eta_seconds = None
            db.commit()
            _publish_job(job, throttle_progress=False)
            return
        # If an abort/cancel request landed while optimize_video was running,
        # never allow final metrics to overwrite it as complete.
        if job.cancel_requested or job.status in {'aborting', 'cancelled'}:
            job.status = 'cancelled'
            job.error_message = job.error_message or 'Aborted by user'
            job.eta_seconds = None
            job.output_path = None
            job.fps = None
            job.cancel_requested = False
            _mark_finished(job)
            db.commit()
            _publish_job(job, throttle_progress=False)
            handle_job_terminal_state(job.id, job.status)
            return
        if job.status == 'failed' and job.error_message == 'Aborted by user':
            _publish_job(job, throttle_progress=False)
            handle_job_terminal_state(job.id, job.status)
            return

        # If the application is shutting down, leave the job in its current
        # DB state (still 'running') so startup recovery can find it and
        # requeue it on next launch.  Committing a terminal status here would
        # move the job to History and it would never be picked up again.
        if stop_event.is_set():
            return

        job.status = metrics.status
        job.output_path = metrics.output_path
        job.fps = metrics.fps
        job.encoder_used = metrics.encoder_used
        job.codec_used = metrics.codec_used
        job.hwaccel_used = metrics.hwaccel_used
        job.used_fallback = metrics.used_fallback
        job.fallback_reason = metrics.fallback_reason
        if metrics.status == 'failed':
            job.error_message = metrics.error_message or metrics.skipped_reason or 'optimization_failed'
            workspace = Path(coerce_workspace_root(settings.workspace_root)) / str(job.id)
            partial_duration = _probe_partial_duration(workspace)
            if partial_duration is not None and partial_duration > 0:
                # Partial output found — save position so progress isn't lost.
                job.resume_position_seconds = partial_duration
                if job.retry_count < 1:
                    # Auto-retry once, resuming from where it left off.
                    job.retry_count += 1
                    job.status = 'queued'
                    job.eta_seconds = None
                    job.completed_at = None
                else:
                    # Already auto-retried — mark failed with position saved.
                    # 'failed' lands in History and shows the Retry button;
                    # resume_position_seconds is preserved so retry picks up
                    # exactly where encoding stopped.
                    job.status = 'failed'
                    _mark_finished(job)
            elif job.retry_count < 1:
                job.retry_count += 1
                job.status = 'queued'
                job.progress_percent = 0
                job.eta_seconds = None
                job.completed_at = None
                job.resume_position_seconds = None
            else:
                _mark_finished(job)
        elif metrics.status == 'complete':
            job.progress_percent = 100
            job.eta_seconds = 0
            _mark_finished(job)
        else:
            _mark_finished(job)
        db.commit()
        _publish_job(job, throttle_progress=False)
        if job.status == 'complete':
            trigger_scan_after_job(job.library_id)
            enqueue_job_complete(job)
        if job.status == 'failed':
            enqueue_job_failed(job)
        handle_job_terminal_state(job.id, job.status)
    except Exception as exc:
        logger.exception('Unhandled exception while processing queued job %s', job_id)
        db.rollback()

        # During application shutdown an exception may be raised by DB or I/O
        # teardown.  Leave the job in its last committed state so startup
        # recovery can requeue it; do not mark it failed.
        if stop_event.is_set():
            return

        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = 'failed'
            job.error_message = str(exc) or exc.__class__.__name__
            _mark_finished(job)
            db.commit()
            _publish_job(job, throttle_progress=False)
            enqueue_job_failed(job)
            handle_job_terminal_state(job.id, job.status)
    finally:
        db.close()
        with _pool_lock:
            _active_workers.pop(job_id, None)


def _is_within_schedule_window(current_hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour <= end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


def _library_job_can_start(settings: Settings, now: datetime, library: Library | None, profile: LibraryProfile | None) -> bool:
    if not settings.enable_optimizer:
        return False

    if library is None:
        return True

    if not library.enabled:
        return False

    if profile is None:
        return True

    if not profile.schedule_enabled:
        return True

    return _is_within_schedule_window(now.hour, profile.schedule_start_hour, profile.schedule_end_hour)


def _library_is_in_schedule_window(now: datetime, profile: LibraryProfile | None) -> bool:
    if profile is None:
        return True
    if not profile.schedule_enabled:
        return True
    return _is_within_schedule_window(now.hour, profile.schedule_start_hour, profile.schedule_end_hour)


def _restart_paused_schedule_jobs(db: Session, settings: Settings, now: datetime) -> None:
    paused_jobs = (
        db.query(Job, LibraryProfile)
        .join(Library, Job.library_id == Library.id)
        .outerjoin(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(Job.status == 'paused_schedule')
        .all()
    )

    for job, profile in paused_jobs:
        if not _library_is_in_schedule_window(now, profile):
            continue

        # Preserve the partial output so the job can resume from its saved
        # position rather than re-encoding from the start.
        job.status = 'queued'
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.error_message = None
        job.cancel_requested = False
        job.completed_at = None
        # resume_position_seconds and progress_percent are kept as-is.
        db.commit()
        _publish_job(job, throttle_progress=False)
        broker.publish_system_event(
            'schedule_policy_state_changed',
            state='resumed_from_schedule',
            job_id=job.id,
            library_id=job.library_id,
        )


def _enforce_library_schedule_policies(db: Session, settings: Settings, now: datetime) -> None:
    _restart_paused_schedule_jobs(db, settings, now)

    running_jobs = (
        db.query(Job, LibraryProfile)
        .join(Library, Job.library_id == Library.id)
        .outerjoin(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(Job.status == 'running')
        .all()
    )

    for job, profile in running_jobs:
        if _library_is_in_schedule_window(now, profile):
            continue
        if profile is None or profile.schedule_policy != SchedulePolicyEnum.pause_current:
            continue

        current_position = get_active_position(job.id)
        stop_active_ffmpeg(job.id)
        db.refresh(job)
        job.status = 'paused_schedule'
        job.cancel_requested = False
        job.eta_seconds = None
        if current_position is not None and current_position > 0:
            job.resume_position_seconds = current_position
        stop_encode_timing(job)
        db.commit()
        _publish_job(job, throttle_progress=False)
        broker.publish_system_event(
            'schedule_policy_state_changed',
            state='paused_schedule',
            job_id=job.id,
            library_id=job.library_id,
        )




def _extract_year_from_path(path: str | None) -> int | None:
    if not path:
        return None

    def extract_year(candidate: str) -> int | None:
        spaced = re.sub(r'[._]', ' ', candidate)
        paren_match = re.search(r'\(((?:19|20)\d{2})\)', spaced)
        if paren_match:
            return int(paren_match.group(1))
        year_match = re.search(r'\b((?:19|20)\d{2})\b', spaced)
        if year_match:
            return int(year_match.group(1))
        return None

    def looks_like_tv_container(candidate: str) -> bool:
        normalized = re.sub(r'[._]', ' ', candidate).strip().lower()
        return bool(
            re.fullmatch(r'season\s*\d+', normalized)
            or re.fullmatch(r'series\s*\d+', normalized)
            or re.fullmatch(r's\d+', normalized)
            or normalized == 'specials'
        )

    resolved = Path(path)
    year = extract_year(resolved.stem)
    if year is not None:
        return year

    direct_parent = resolved.parent.name
    candidate_parent = resolved.parent.parent.name if direct_parent and looks_like_tv_container(direct_parent) else direct_parent
    if candidate_parent:
        year = extract_year(candidate_parent)
        if year is not None:
            return year
    return None


def _claim_next_queued_job(db: Session, settings: Settings, now: datetime) -> int | None:
    raw_sort_option = getattr(settings, 'queue_sort', QueueSortEnum.default) or QueueSortEnum.default
    sort_option = str(getattr(raw_sort_option, 'value', raw_sort_option))
    base_query = (
        db.query(Job, Library, LibraryProfile)
        .outerjoin(Library, Job.library_id == Library.id)
        .outerjoin(LibraryProfile, LibraryProfile.library_id == Library.id)
        .filter(Job.status == 'queued')
    )

    _ACTIVE_DOWNLOAD_STATUSES = (
        DownloadJobStatus.pending.value,
        DownloadJobStatus.searching.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
    )
    active_download_rows = (
        db.query(DownloadJob.id, DownloadJob.source_file_path, DownloadJob.status)
        .filter(DownloadJob.status.in_(_ACTIVE_DOWNLOAD_STATUSES))
        .all()
    )
    active_download_sources = {
        source_path
        for _, source_path, _ in active_download_rows
        if source_path
    }
    active_download_by_source = {
        source_path: (download_id, status)
        for download_id, source_path, status in active_download_rows
        if source_path
    }

    if sort_option in {QueueSortEnum.default.value, QueueSortEnum.oldest.value, QueueSortEnum.newest.value}:
        if sort_option == QueueSortEnum.newest.value:
            queued = base_query.order_by(Job.created_at.desc(), Job.id.desc()).all()
        else:
            queued = base_query.order_by(Job.created_at.asc(), Job.id.asc()).all()
    else:
        queued = base_query.all()

        def sort_key(row: tuple[Job, Library | None, LibraryProfile | None]) -> tuple:
            job = row[0]
            if sort_option == QueueSortEnum.year_newest.value:
                year = _extract_year_from_path(job.source_path)
                return (-(year if year is not None else 0), -job.id)
            if sort_option == QueueSortEnum.year_oldest.value:
                year = _extract_year_from_path(job.source_path)
                return ((year if year is not None else 9999), job.id)

            return (job.created_at.timestamp() if job.created_at else 0, job.id)

        queued.sort(key=sort_key)

    for job, library, profile in queued:
        if not _library_job_can_start(settings, now, library, profile):
            continue

        # If this library uses download mode and the source file is still
        # being searched for or downloaded, hold off on encoding it.
        if getattr(profile, 'download_enabled', False) and job.source_path:
            from app.services.download_monitor_service import (
                can_attempt_download,
                create_download_job,
                recover_completed_artifact_for_queue_job,
                download_job_exists_for_source,
            )
            if recover_completed_artifact_for_queue_job(db, job, library, profile):
                logger.info(
                    'Queue precheck: imported completed artifact for queued job %s source=%r',
                    job.id,
                    job.source_path,
                )
            completed_import_row = (
                db.query(DownloadJob.id)
                .filter(
                    DownloadJob.source_file_path == job.source_path,
                    DownloadJob.library_id == job.library_id,
                    DownloadJob.status == DownloadJobStatus.complete.value,
                    DownloadJob.imported_file_path.isnot(None),
                )
                .order_by(DownloadJob.id.desc())
                .first()
            )
            if completed_import_row is not None:
                completed_download_id = int(completed_import_row[0])
                logger.info(
                    'Queue cleanup: removing stale encode placeholder job %s for source %r '
                    'because download job %s already completed import',
                    job.id,
                    job.source_path,
                    completed_download_id,
                )
                placeholder_job_id = job.id
                db.delete(job)
                db.commit()
                broker.publish_system_event('job_removed', job_id=placeholder_job_id)
                continue
            if job.source_path in active_download_sources:
                dj_id, dj_status = active_download_by_source.get(job.source_path, (None, None))
                logger.info(
                    'Queue hold: encode job %s for %r is waiting on download job %s (%s)',
                    job.id,
                    job.source_path,
                    dj_id,
                    dj_status,
                )
                continue
            # Safety net: if a queued encode job exists for a download-enabled
            # library and the download route is available, create the download
            # job now and keep the encode job queued as a placeholder.
            if can_attempt_download(db):
                terminal_download_row = (
                    db.query(DownloadJob.id, DownloadJob.status)
                    .filter(
                        DownloadJob.source_file_path == job.source_path,
                        DownloadJob.encode_job_id == job.id,
                        DownloadJob.status.in_([
                            DownloadJobStatus.failed.value,
                            DownloadJobStatus.timed_out.value,
                            DownloadJobStatus.waiting_encode.value,
                            DownloadJobStatus.fallback_queued.value,
                        ]),
                    )
                    .order_by(DownloadJob.id.desc())
                    .first()
                )
                if terminal_download_row is not None:
                    terminal_dj_id, terminal_dj_status = terminal_download_row
                    logger.info(
                        'Queue routing bypass: queued encode job %s is linked to terminal download job %s (%s); '
                        'starting encode without re-routing',
                        job.id,
                        terminal_dj_id,
                        terminal_dj_status,
                    )
                    # This queued encode row is the intended fallback path.
                    # Do not re-create a download job for the same source.
                elif not download_job_exists_for_source(db, job.source_path):
                    create_download_job(db, job.source_path, library, profile)
                    logger.info(
                        'Queue routing: created download job for queued encode job %s source=%r',
                        job.id,
                        job.source_path,
                    )
                    # This queued encode row was only a routing placeholder.
                    # Remove it so queue UI does not show a duplicate row.
                    placeholder_job_id = job.id
                    db.delete(job)
                    db.commit()
                    broker.publish_system_event('job_removed', job_id=placeholder_job_id)
                    continue
                else:
                    continue
            elif download_job_exists_for_source(db, job.source_path):
                logger.info(
                    'Queue hold: encode job %s for %r skipped because a download job already exists',
                    job.id,
                    job.source_path,
                )
                continue

        job.status = 'starting'
        db.commit()
        _publish_job(job, throttle_progress=False)
        return job.id

    return None


def _persist_queue_paused(paused: bool) -> None:
    try:
        db = SessionLocal()
        try:
            settings = db.query(Settings).first()
            if settings is not None:
                settings.queue_paused = paused
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning('Failed to persist queue pause state', exc_info=True)


def pause_queue(reason: str = 'manual') -> None:
    global _queue_paused
    if _queue_paused:
        return
    _queue_paused = True
    if reason == 'manual':
        _persist_queue_paused(True)
    broker.publish_system_event('queue_paused', reason=reason)


def resume_queue(reason: str = 'manual') -> None:
    global _queue_paused
    if not _queue_paused:
        return
    _queue_paused = False
    if reason == 'manual':
        _persist_queue_paused(False)
    broker.publish_system_event('queue_resumed', reason=reason)


def is_queue_paused() -> bool:
    return _queue_paused


def start_queued_job(job_id: int, *, manual: bool = False) -> tuple[bool, str | None]:
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        row = (
            db.query(Job, Library, LibraryProfile)
            .outerjoin(Library, Job.library_id == Library.id)
            .outerjoin(LibraryProfile, LibraryProfile.library_id == Library.id)
            .filter(Job.id == job_id)
            .first()
        )
        if row is None:
            return False, 'Job not found'

        job, library, profile = row
        if job.status != 'queued':
            return False, f'Job status is {job.status}'

        if not bool(settings.enable_optimizer) and not manual:
            return False, 'Optimizer is disabled'

        if not manual and not _library_job_can_start(settings, datetime.now(), library, profile):
            return False, 'Library schedule or availability prevents start'

        with _pool_lock:
            if job_id in _active_workers:
                return False, 'Job is already active'

            max_workers = max(1, int(settings.max_workers))
            if len(_active_workers) >= max_workers:
                if not manual:
                    return False, 'Maximum workers already running'

                # Manual starts are intentionally preemptive: pause a currently
                # running job so the explicitly selected job can begin.
                running_job = (
                    db.query(Job)
                    .filter(Job.id != job_id, Job.status == 'running')
                    .order_by(Job.created_at.asc())
                    .first()
                )
                if running_job is None:
                    return False, 'Maximum workers already running'

                current_position = get_active_position(running_job.id)
                stop_active_ffmpeg(running_job.id)
                db.refresh(running_job)
                running_job.status = 'paused'
                running_job.cancel_requested = False
                running_job.eta_seconds = None
                if current_position is not None and current_position > 0:
                    running_job.resume_position_seconds = current_position
                stop_encode_timing(running_job)
                db.commit()
                _publish_job(running_job, throttle_progress=False)
                broker.publish_system_event('job_paused', job_id=running_job.id)

            job.status = 'starting'
            db.commit()
            _publish_job(job, throttle_progress=False)

            worker = Thread(target=_process_job, args=(job_id,), daemon=True, name=f'optimizer-job-{job_id}')
            worker.start()
            _active_workers[job_id] = worker
        return True, None
    finally:
        db.close()


def _should_workers_run(settings: Settings, now: datetime) -> bool:
    _ = now
    if _queue_paused:
        return False
    return bool(settings.enable_optimizer)


def _manager_loop() -> None:
    global _workers_allowed
    global _last_prune_at
    global _last_schedule_check_at
    global _last_workers_allowed
    while not stop_event.is_set():
        loop_sleep_seconds = 0.2
        launched_worker = False
        db = SessionLocal()
        try:
            settings = _get_settings(db)
            _workers_allowed = _should_workers_run(settings, datetime.now())
            if _last_workers_allowed is None:
                _last_workers_allowed = _workers_allowed
            elif _workers_allowed != _last_workers_allowed:
                if not _queue_paused:
                    if _workers_allowed:
                        broker.publish_system_event('queue_resumed', reason='schedule')
                    else:
                        broker.publish_system_event('queue_paused', reason='schedule')
                _last_workers_allowed = _workers_allowed

            now_monotonic = time.monotonic()
            if now_monotonic - _last_prune_at >= 60:
                deleted = prune_job_history(db, int(settings.history_retention_days))
                if deleted:
                    logger.info('Pruned %s stale completed jobs', deleted)
                _last_prune_at = now_monotonic

            if now_monotonic - _last_schedule_check_at >= 60:
                _enforce_library_schedule_policies(db, settings, datetime.now())
                _last_schedule_check_at = now_monotonic

            max_workers = max(1, int(settings.max_workers))

            if _workers_allowed:
                while True:
                    with _pool_lock:
                        active_count = len(_active_workers)
                    if active_count >= max_workers:
                        break

                    next_job_id = _claim_next_queued_job(db, settings, datetime.now())
                    if not next_job_id:
                        break

                    worker = Thread(target=_process_job, args=(next_job_id,), daemon=True, name=f'optimizer-job-{next_job_id}')
                    worker.start()
                    launched_worker = True
                    with _pool_lock:
                        _active_workers[next_job_id] = worker
            else:
                # Paused/disabled queue: back off to reduce idle DB churn.
                loop_sleep_seconds = 1.0
        finally:
            db.close()

        if _workers_allowed and not launched_worker:
            # Nothing started this cycle; poll more slowly while idle.
            loop_sleep_seconds = 1.0

        time.sleep(loop_sleep_seconds)


def start_worker() -> Thread:
    global _manager_thread
    if _manager_thread and _manager_thread.is_alive():
        return _manager_thread

    stop_event.clear()
    _manager_thread = Thread(target=_manager_loop, name='optimizer-manager', daemon=True)
    _manager_thread.start()
    return _manager_thread


def stop_worker() -> None:
    global _manager_thread
    global _last_workers_allowed
    stop_event.set()

    if _manager_thread and _manager_thread.is_alive():
        _manager_thread.join(timeout=5)

    with _pool_lock:
        workers = list(_active_workers.values())
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=30)

    _manager_thread = None
    _last_workers_allowed = None
