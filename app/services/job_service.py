from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path
import subprocess

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.library import LibraryProfile
from app.models.settings import Settings
from app.services import optimization_service

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def _profile_snapshot(profile: LibraryProfile | None) -> str | None:
    if profile is None:
        return None

    return json.dumps(
        {
            'target_resolution': profile.target_resolution,
            'minimum_source_resolution': profile.minimum_source_resolution,
            'codec': profile.codec.value,
            'container': profile.container.value,
            'audio_mode': profile.audio_mode.value,
            'bitrate_mode': profile.bitrate_mode.value,
            'bitrate_mbps': profile.bitrate_mbps,
            'crf': profile.crf,
            'speed_preset': profile.speed_preset.value,
            'hdr_only': profile.hdr_only,
            'tone_map_hdr': profile.tone_map_hdr,
            'max_workers': profile.max_workers,
            'schedule_enabled': profile.schedule_enabled,
            'schedule_start_hour': profile.schedule_start_hour,
            'schedule_end_hour': profile.schedule_end_hour,
            'schedule_policy': profile.schedule_policy.value,
            'output_suffix': profile.output_suffix,
            'output_conflict_policy': profile.output_conflict_policy.value,
            'av1_fallback_codec': profile.av1_fallback_codec.value,
            'preferred_video_encoder': profile.preferred_video_encoder.value,
        }
    )


def create_job(
    db: Session,
    source_path: str,
    library_id: int | None = None,
    profile: LibraryProfile | None = None,
    source_resolution: int | None = None,
    source_is_hdr: bool | None = None,
    status: str = 'queued',
) -> Job:
    job = Job(
        input_path=source_path,
        status=status,
        library_id=library_id,
        profile_snapshot_json=_profile_snapshot(profile),
        source_resolution=source_resolution,
        source_is_hdr=source_is_hdr,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def refresh_queued_job_snapshots(db: Session, library_id: int, profile: LibraryProfile) -> int:
    """Re-snapshot the profile onto every queued job for a library.

    Called after a library profile is updated so that jobs waiting in the queue
    pick up the new settings (e.g. speed_preset, codec, bitrate) rather than
    the stale snapshot that was taken when they were originally created.

    Returns the number of jobs updated.
    """
    snapshot = _profile_snapshot(profile)
    updated = (
        db.query(Job)
        .filter(Job.library_id == library_id, Job.status == 'queued')
        .all()
    )
    for job in updated:
        job.profile_snapshot_json = snapshot
    if updated:
        db.commit()
    return len(updated)


def job_exists_for_source(db: Session, source_path: str, library_id: int | None = None) -> bool:
    # 'complete' is intentionally excluded so successfully-finished jobs prevent re-queuing.
    # Only failed/skipped/cancelled jobs are retryable on the next scan.
    _RETRYABLE_STATUSES = {'failed', 'skipped', 'cancelled'}
    query = db.query(Job).filter(Job.input_path == source_path, ~Job.status.in_(_RETRYABLE_STATUSES))
    if library_id is None:
        query = query.filter(Job.library_id.is_(None))
    else:
        query = query.filter(Job.library_id == library_id)
    return db.query(query.exists()).scalar()


def get_job(db: Session, job_id: int) -> Job | None:
    return db.query(Job).filter(Job.id == job_id).first()


def delete_job(db: Session, job_id: int) -> bool:
    job = get_job(db, job_id)
    if not job:
        return False

    db.delete(job)
    db.commit()
    return True


def list_jobs(db: Session) -> list[Job]:
    return db.query(Job).order_by(Job.created_at.desc()).all()


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if settings:
        return settings

    settings = Settings()
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def _probe_partial_duration(workspace: Path) -> float | None:
    if not workspace.exists():
        return None
    partials = list(workspace.glob('output.partial.*'))
    if not partials:
        return None

    command = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(partials[0]),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None

    values = result.stdout.strip().splitlines()
    if not values:
        return None
    try:
        return float(values[-1].strip())
    except ValueError:
        return None


def cancel_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status in TERMINAL_STATUSES:
        return job

    job.cancel_requested = True
    if job.status == 'queued':
        job.status = 'cancelled'
        job.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def retry_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status not in {'failed', 'cancelled'}:
        return job

    settings = _get_settings(db)
    if not job.resume_position_seconds:
        workspace = Path(settings.workspace_root) / str(job.id)
        partial_duration = _probe_partial_duration(workspace)
        if partial_duration and partial_duration > 0:
            job.resume_position_seconds = partial_duration
        else:
            job.progress_percent = 0

    job.status = 'queued'
    job.retry_count = 0
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    db.commit()
    db.refresh(job)
    return job


def pause_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status != 'running':
        return job

    # Capture the current encode position before terminating FFmpeg so we can
    # resume from that offset instead of re-encoding from the beginning.
    current_position = optimization_service.get_active_position(job_id)
    optimization_service.stop_active_ffmpeg(job_id)

    job.status = 'paused'
    job.cancel_requested = False
    job.eta_seconds = None
    if current_position is not None and current_position > 0:
        job.resume_position_seconds = current_position
    db.commit()
    db.refresh(job)
    return job


def resume_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status != 'paused':
        return job

    # Do NOT delete the partial output — it will be used to resume encoding from
    # the saved position rather than starting over from the beginning.
    job.status = 'queued'
    job.fps = None
    job.eta_seconds = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    # resume_position_seconds and progress_percent are intentionally preserved so
    # optimize_video knows where to seek and the UI shows existing progress.
    db.commit()
    db.refresh(job)
    return job


def abort_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    settings = _get_settings(db)
    optimization_service.stop_active_ffmpeg(job_id)
    optimization_service.delete_workspace(settings, job_id)
    job.status = 'failed'
    job.progress_percent = 0
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = 'Aborted by user'
    job.cancel_requested = False
    job.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def discard_progress_and_requeue(db: Session, job_id: int) -> Job | None:
    """Stop the job (if running), wipe its partial workspace and progress
    data, then return it to the queue so it restarts from the beginning.

    This is offered as an alternative to a full abort when the job is paused
    and has partial progress – the user can choose to keep the item in the
    queue rather than removing it entirely.
    """
    job = get_job(db, job_id)
    if not job:
        return None

    settings = _get_settings(db)
    optimization_service.stop_active_ffmpeg(job_id)
    optimization_service.delete_workspace(settings, job_id)

    job.status = 'queued'
    job.progress_percent = 0
    job.resume_position_seconds = None
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    job.retry_count = 0
    db.commit()
    db.refresh(job)
    return job


def prune_job_history(db: Session, retention_days: int) -> int:
    if retention_days <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    stale_jobs = (
        db.query(Job)
        .filter(Job.status.in_(TERMINAL_STATUSES), Job.completed_at.is_not(None), Job.completed_at < cutoff)
        .all()
    )

    deleted_count = len(stale_jobs)
    for stale_job in stale_jobs:
        db.delete(stale_job)

    if deleted_count:
        db.commit()

    return deleted_count




def cleanup_optimized_outputs(db: Session) -> tuple[int, list[int]]:
    terminal_jobs = db.query(Job).filter(Job.status.in_(TERMINAL_STATUSES), Job.output_path.is_not(None)).all()

    removed_files = 0
    removed_job_ids: list[int] = []
    for job in terminal_jobs:
        output_path = str(job.output_path or '').strip()
        if not output_path:
            continue

        candidate = Path(output_path)
        if not candidate.exists() or not candidate.is_file():
            continue

        candidate.unlink(missing_ok=True)
        if not candidate.exists():
            removed_files += 1
            removed_job_ids.append(job.id)

    return removed_files, removed_job_ids


def abort_all_jobs(db: Session) -> list[Job]:
    targets = db.query(Job).filter(~Job.status.in_(TERMINAL_STATUSES)).all()
    if not targets:
        return []

    settings = _get_settings(db)
    now = datetime.utcnow()
    for job in targets:
        optimization_service.stop_active_ffmpeg(job.id)
        optimization_service.delete_workspace(settings, job.id)
        job.status = 'failed'
        job.progress_percent = 0
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.error_message = 'Aborted by user'
        job.cancel_requested = False
        job.completed_at = now

    db.commit()
    for job in targets:
        db.refresh(job)
    return targets


def remove_all_terminal_jobs(db: Session) -> list[int]:
    terminal_jobs = db.query(Job).filter(Job.status.in_(TERMINAL_STATUSES)).all()
    if not terminal_jobs:
        return []

    removed_job_ids = [job.id for job in terminal_jobs]
    for job in terminal_jobs:
        db.delete(job)
    db.commit()
    return removed_job_ids


def cancel_all_queued_jobs(db: Session) -> list[Job]:
    targets = db.query(Job).filter(Job.status == 'queued').all()
    if not targets:
        return []

    now = datetime.utcnow()
    for job in targets:
        job.status = 'cancelled'
        job.completed_at = now
    db.commit()
    for job in targets:
        db.refresh(job)
    return targets
