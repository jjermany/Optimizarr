from pathlib import Path

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.settings import Settings
from app.services import recovery_service


def _configure_settings(db, workspace_root: Path, *, requeue: bool = True) -> Settings:
    db.query(Settings).delete()
    settings = Settings(
        workspace_root=str(workspace_root),
        requeue_interrupted_jobs=requeue,
        cleanup_workspaces_on_startup=True,
    )
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def test_startup_recovery_requeues_starting_jobs(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, tmp_path / 'workspaces', requeue=True)

        job = Job(input_path='/media/restart-mid-starting.mkv', status='starting', progress_percent=19)
        db.add(job)
        db.commit()
        db.refresh(job)

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: None)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(job)

        assert summary['recovered_jobs'] == 1
        assert summary['requeued_jobs'] == 1
        assert summary['interrupted_job_ids'] == [job.id]
        assert job.status == 'queued'
        assert job.progress_percent == 0
        assert job.resume_position_seconds is None
        assert job.error_message == 'Interrupted by application restart'


def test_startup_recovery_preserves_partial_resume_state_when_available(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'

    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, workspace_root, requeue=True)

        job = Job(input_path='/media/restart-running.mkv', status='running', progress_percent=61)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 123.4)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(job)

        assert summary['recovered_jobs'] == 1
        assert summary['requeued_jobs'] == 1
        assert summary['cleaned_workspaces'] == 0
        assert job.status == 'queued'
        assert job.progress_percent == 61
        assert job.resume_position_seconds == 123.4
        assert workspace.exists()
