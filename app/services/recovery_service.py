from __future__ import annotations

from pathlib import Path
import shutil

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.settings import Settings

RECOVERABLE_STATUSES = {'running', 'preflight', 'aborting'}


def _get_or_create_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if settings:
        return settings

    settings = Settings()
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def _workspace_path(settings: Settings, job_id: int) -> Path:
    return Path(settings.workspace_root) / str(job_id)


def run_startup_recovery(db: Session) -> dict[str, int | list[int]]:
    settings = _get_or_create_settings(db)
    jobs = db.query(Job).filter(Job.status.in_(RECOVERABLE_STATUSES)).all()

    recovered_jobs = 0
    cleaned_workspaces = 0
    requeued_jobs = 0
    interrupted_job_ids: list[int] = []

    for job in jobs:
        recovered_jobs += 1
        interrupted_job_ids.append(job.id)
        job.status = 'interrupted'
        job.cancel_requested = False
        job.error_message = 'Interrupted by application restart'
        job.completed_at = None

        workspace = _workspace_path(settings, job.id)
        if settings.cleanup_workspaces_on_startup and workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            cleaned_workspaces += 1

        if settings.requeue_interrupted_jobs:
            job.status = 'queued'
            job.progress_percent = 0
            job.fps = None
            job.eta_seconds = None
            job.output_path = None
            requeued_jobs += 1

    if recovered_jobs:
        db.commit()

    return {
        'recovered_jobs': recovered_jobs,
        'requeued_jobs': requeued_jobs,
        'cleaned_workspaces': cleaned_workspaces,
        'interrupted_job_ids': interrupted_job_ids,
    }
