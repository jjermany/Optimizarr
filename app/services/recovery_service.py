from __future__ import annotations

from pathlib import Path
import json
import shutil

from sqlalchemy.orm import Session

from app.core.security import coerce_workspace_root
from app.models.job import Job
from app.models.settings import Settings
from app.services.job_timing_service import stop_encode_timing
from app.services import optimization_service

RECOVERABLE_STATUSES = {'running', 'preflight', 'starting', 'aborting', 'paused', 'paused_schedule'}
ACTIVE_WORKSPACE_STATUSES = {'running', 'preflight', 'starting', 'aborting', 'paused', 'paused_schedule'}
LEGACY_QUEUED_STATUSES = {'pending', 'created'}
STALE_WORKSPACE_ARTIFACT_PATTERNS = (
    'output.resume.*',
    'output.combined.*',
    'output.partial.recovered*',
    'concat_list.txt',
)


def _apply_partial_resume_if_available(job: Job, workspace: Path, partial_duration: float | None) -> bool:
    if partial_duration and partial_duration > 0:
        job.resume_position_seconds = partial_duration
        return True

    job.resume_position_seconds = None
    job.progress_percent = 0
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    return False




def _pin_resume_encoder(job: Job) -> None:
    """Lock resumable jobs to their last known working codec/encoder."""
    if not job.resume_position_seconds or not job.profile_snapshot_json:
        return

    try:
        snapshot = json.loads(job.profile_snapshot_json)
    except json.JSONDecodeError:
        return

    if not isinstance(snapshot, dict):
        return

    changed = False
    if job.codec_used:
        codec = str(job.codec_used).lower()
        if snapshot.get('codec') != codec:
            snapshot['codec'] = codec
            changed = True
    if job.encoder_used:
        encoder = str(job.encoder_used).lower()
        if snapshot.get('preferred_video_encoder') != encoder:
            snapshot['preferred_video_encoder'] = encoder
            changed = True

    if changed:
        job.profile_snapshot_json = json.dumps(snapshot)

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
    return Path(coerce_workspace_root(settings.workspace_root)) / str(job_id)


def _probe_partial_duration(workspace: Path) -> float | None:
    return optimization_service.recover_resume_position(workspace)


def _cleanup_stale_workspace_artifacts(workspace: Path) -> int:
    removed = 0
    if not workspace.exists():
        return removed
    for pattern in STALE_WORKSPACE_ARTIFACT_PATTERNS:
        for candidate in workspace.glob(pattern):
            if not candidate.exists():
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
            else:
                candidate.unlink(missing_ok=True)
            if not candidate.exists():
                removed += 1
    return removed


def run_startup_recovery(db: Session) -> dict[str, int | list[int]]:
    settings = _get_or_create_settings(db)
    jobs = db.query(Job).filter(Job.status.in_(RECOVERABLE_STATUSES)).all()

    recovered_jobs = 0
    cleaned_workspaces = 0
    requeued_jobs = 0
    interrupted_job_ids: list[int] = []

    for job in jobs:
        stop_encode_timing(job, include_idle_since_last_activity=False)
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
            job.retry_count = 0
            job.fps = None
            job.eta_seconds = None
            job.output_path = None
            requeued_jobs += 1

            workspace_existed = workspace.exists()
            if not _apply_partial_resume_if_available(job, workspace, partial_duration) and workspace_existed:
                cleaned_workspaces += 1
            _pin_resume_encoder(job)
        else:
            job.resume_position_seconds = None
            job.progress_percent = 0
            if settings.cleanup_workspaces_on_startup and workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
                cleaned_workspaces += 1

    legacy_queued_jobs = db.query(Job).filter(Job.status.in_(LEGACY_QUEUED_STATUSES)).all()
    for job in legacy_queued_jobs:
        job.status = 'queued'
        job.progress_percent = 0
        job.retry_count = 0
        job.cancel_requested = False
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.error_message = None
        job.completed_at = None
        requeued_jobs += 1

    # SessionLocal is configured with autoflush=False, so flush status changes
    # before querying queued rows for partial-resume attachment.
    db.flush()

    queued_jobs = db.query(Job).filter(Job.status == 'queued').all()
    for job in queued_jobs:
        if job.resume_position_seconds:
            continue

        workspace = _workspace_path(settings, job.id)
        if not workspace.exists():
            continue

        partial_duration = _probe_partial_duration(workspace)
        if _apply_partial_resume_if_available(job, workspace, partial_duration):
            _pin_resume_encoder(job)
            requeued_jobs += 1
        else:
            cleaned_workspaces += 1

    if recovered_jobs or requeued_jobs or cleaned_workspaces:
        db.commit()

    return {
        'recovered_jobs': recovered_jobs,
        'requeued_jobs': requeued_jobs,
        'cleaned_workspaces': cleaned_workspaces,
        'interrupted_job_ids': interrupted_job_ids,
    }


def requeue_interrupted_job(db: Session, job_id: int) -> Job | None:
    """Re-queue an interrupted job, resuming from its partial output if available."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or job.status != 'interrupted':
        return job

    settings = _get_or_create_settings(db)
    workspace = _workspace_path(settings, job.id)

    partial_duration = _probe_partial_duration(workspace) if workspace.exists() else None

    job.status = 'queued'
    job.retry_count = 0
    job.cancel_requested = False
    job.error_message = None
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.completed_at = None

    _apply_partial_resume_if_available(job, workspace, partial_duration)
    _pin_resume_encoder(job)

    db.commit()
    db.refresh(job)
    return job


def run_workspace_cleanup(db: Session) -> dict[str, int | list[int]]:
    settings = _get_or_create_settings(db)
    workspace_root = Path(coerce_workspace_root(settings.workspace_root))
    if not workspace_root.exists():
        return {'cleaned_workspaces': 0, 'cleaned_workspace_job_ids': [], 'cleaned_workspace_artifacts': 0}

    active_job_ids = {
        job_id
        for (job_id,) in db.query(Job.id).filter(Job.status.in_(ACTIVE_WORKSPACE_STATUSES)).all()
    }

    resumable_queued_ids: set[int] = set()
    cleaned_workspaces = 0
    cleaned_workspace_job_ids: list[int] = []
    cleaned_workspace_artifacts = 0
    changed_resume_state = False
    resumable_queued_jobs = (
        db.query(Job)
        .filter(
            Job.status == 'queued',
            Job.resume_position_seconds.isnot(None),
        )
        .all()
    )
    for job in resumable_queued_jobs:
        workspace = _workspace_path(settings, job.id)
        partial_duration = _probe_partial_duration(workspace) if workspace.exists() else None
        if partial_duration and partial_duration > 0:
            job.resume_position_seconds = partial_duration
            resumable_queued_ids.add(job.id)
            cleaned_workspace_artifacts += _cleanup_stale_workspace_artifacts(workspace)
            continue

        job.resume_position_seconds = None
        job.progress_percent = 0
        changed_resume_state = True
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            cleaned_workspaces += 1
            cleaned_workspace_job_ids.append(job.id)

    protected_job_ids = active_job_ids | resumable_queued_ids

    for workspace in workspace_root.iterdir():
        if not workspace.is_dir():
            continue
        if not workspace.name.isdigit():
            continue

        job_id = int(workspace.name)
        if job_id in protected_job_ids:
            continue

        shutil.rmtree(workspace, ignore_errors=True)
        cleaned_workspaces += 1
        cleaned_workspace_job_ids.append(job_id)

    if cleaned_workspaces or cleaned_workspace_artifacts or changed_resume_state:
        db.commit()

    return {
        'cleaned_workspaces': cleaned_workspaces,
        'cleaned_workspace_job_ids': cleaned_workspace_job_ids,
        'cleaned_workspace_artifacts': cleaned_workspace_artifacts,
    }
