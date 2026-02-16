from sqlalchemy.orm import Session

from app.models.job import OptimizationJob


def create_job(db: Session, source_path: str) -> OptimizationJob:
    job = OptimizationJob(source_path=source_path, status='queued')
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: int) -> OptimizationJob | None:
    return db.query(OptimizationJob).filter(OptimizationJob.id == job_id).first()


def list_jobs(db: Session) -> list[OptimizationJob]:
    return db.query(OptimizationJob).order_by(OptimizationJob.created_at.desc()).all()
