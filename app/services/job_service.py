from datetime import datetime

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
