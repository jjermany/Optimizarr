from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path

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
) -> Job:
    job = Job(
        input_path=source_path,
        status='queued',
        library_id=library_id,
        profile_snapshot_json=_profile_snapshot(profile),
        source_resolution=source_resolution,
        source_is_hdr=source_is_hdr,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def job_exists_for_source(db: Session, source_path: str, library_id: int | None = None) -> bool:
    query = db.query(Job).filter(Job.input_path == source_path)
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

    if job.retry_count >= 1:
        return job

    job.status = 'queued'
    job.progress_percent = 0
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

    optimization_service.stop_active_ffmpeg(job_id)
    job.status = 'paused'
    job.cancel_requested = False
    job.eta_seconds = None
    db.commit()
    db.refresh(job)
    return job


def resume_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status != 'paused':
        return job

    settings = _get_settings(db)
    optimization_service.delete_partial_output(settings, job_id)
    job.status = 'queued'
    job.progress_percent = 0
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
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
