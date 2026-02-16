from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models.job import Job

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def create_job(db: Session, source_path: str) -> Job:
    job = Job(input_path=source_path, status='queued')
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: int) -> Job | None:
    return db.query(Job).filter(Job.id == job_id).first()


def list_jobs(db: Session) -> list[Job]:
    return db.query(Job).order_by(Job.created_at.desc()).all()


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
