from sqlalchemy.orm import Session

from app.models.job import Job


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
