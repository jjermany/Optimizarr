from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.settings import Settings

RECOVERABLE_STATUSES = {'running', 'preflight', 'starting', 'aborting'}
ACTIVE_WORKSPACE_STATUSES = {'running', 'preflight', 'starting', 'aborting', 'paused', 'paused_schedule'}


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


def _probe_partial_duration(workspace: Path) -> float | None:
    """Return the duration (seconds) of the partial output file in the workspace, if any."""
    partials = list(workspace.glob('output.partial.*'))
    if not partials:
        return None
    partial_path = partials[0]
    command = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(partial_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()
    if not value:
        return None
    try:
        return float(value[-1].strip())
    except ValueError:
        return None


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

        # Probe the partial output for a resume position before any cleanup.
        partial_duration: float | None = None
        if workspace.exists():
            partial_duration = _probe_partial_duration(workspace)

        if settings.requeue_interrupted_jobs:
            job.status = 'queued'
            job.fps = None
            job.eta_seconds = None
            job.output_path = None
            requeued_jobs += 1

            if partial_duration and partial_duration > 0:
                # Keep the workspace so optimize_video can resume from the partial.
                job.resume_position_seconds = partial_duration
                # Leave progress_percent as-is so the UI shows existing progress.
            else:
                # No usable partial; reset progress and clean the workspace.
                job.resume_position_seconds = None
                job.progress_percent = 0
                if workspace.exists():
                    shutil.rmtree(workspace, ignore_errors=True)
                    cleaned_workspaces += 1
        else:
            job.resume_position_seconds = None
            job.progress_percent = 0
            if settings.cleanup_workspaces_on_startup and workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
                cleaned_workspaces += 1

    if recovered_jobs:
        db.commit()

    return {
        'recovered_jobs': recovered_jobs,
        'requeued_jobs': requeued_jobs,
        'cleaned_workspaces': cleaned_workspaces,
        'interrupted_job_ids': interrupted_job_ids,
    }


def run_workspace_cleanup(db: Session) -> dict[str, int | list[int]]:
    settings = _get_or_create_settings(db)
    workspace_root = Path(settings.workspace_root)
    if not workspace_root.exists():
        return {'cleaned_workspaces': 0, 'cleaned_workspace_job_ids': []}

    active_job_ids = {
        job_id
        for (job_id,) in db.query(Job.id).filter(Job.status.in_(ACTIVE_WORKSPACE_STATUSES)).all()
    }

    cleaned_workspaces = 0
    cleaned_workspace_job_ids: list[int] = []
    for workspace in workspace_root.iterdir():
        if not workspace.is_dir():
            continue
        if not workspace.name.isdigit():
            continue

        job_id = int(workspace.name)
        if job_id in active_job_ids:
            continue

        shutil.rmtree(workspace, ignore_errors=True)
        cleaned_workspaces += 1
        cleaned_workspace_job_ids.append(job_id)

    return {
        'cleaned_workspaces': cleaned_workspaces,
        'cleaned_workspace_job_ids': cleaned_workspace_job_ids,
    }
