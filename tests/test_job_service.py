from datetime import datetime, timedelta

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.models.settings import Settings
from app.services import optimization_service
from app.services.job_service import cancel_job, create_job, pause_job, prune_job_history, refresh_queued_job_snapshots, resume_job, retry_job


def test_create_job_stores_profile_snapshot():
    with SessionLocal() as db:
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, codec='av1', av1_fallback_codec='h264', output_suffix='-opt')
        db.add(profile)
        db.commit()
        db.refresh(profile)

        job = create_job(db, '/media/movies/title.mkv', library_id=library.id, profile=profile)
        assert job.library_id == library.id
        assert job.profile_snapshot_json is not None
        assert '"codec": "av1"' in job.profile_snapshot_json
        assert '"output_suffix": "-opt"' in job.profile_snapshot_json
        assert '"minimum_source_resolution"' in job.profile_snapshot_json


def test_prune_job_history_removes_stale_terminal_jobs():
    with SessionLocal() as db:
        stale_terminal = Job(
            input_path='/media/old.mkv',
            status='complete',
            completed_at=datetime.utcnow() - timedelta(days=40),
        )
        fresh_terminal = Job(
            input_path='/media/new.mkv',
            status='failed',
            completed_at=datetime.utcnow() - timedelta(days=1),
        )
        stale_active = Job(
            input_path='/media/running.mkv',
            status='running',
            completed_at=datetime.utcnow() - timedelta(days=40),
        )
        db.add_all([stale_terminal, fresh_terminal, stale_active])
        db.commit()

        deleted = prune_job_history(db, retention_days=30)
        assert deleted >= 1

        remaining_ids = {job.id for job in db.query(Job).all()}
        assert stale_terminal.id not in remaining_ids
        assert fresh_terminal.id in remaining_ids
        assert stale_active.id in remaining_ids

        db.delete(fresh_terminal)
        db.delete(stale_active)
        db.commit()


def test_pause_job_captures_resume_position_without_resetting_progress(monkeypatch):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(input_path='/media/pause-me.mkv', status='running', progress_percent=44, eta_seconds=120)
        db.add(job)
        db.commit()
        db.refresh(job)

        stopped_job_ids: list[int] = []
        monkeypatch.setattr(optimization_service, 'get_active_position', lambda job_id: 133.7)
        monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped_job_ids.append(job_id))

        updated = pause_job(db, job.id)

        assert updated is not None
        assert updated.status == 'paused'
        assert updated.progress_percent == 44
        assert updated.resume_position_seconds == 133.7
        assert updated.eta_seconds is None
        assert stopped_job_ids == [job.id]


def test_cancel_job_requeues_queued_job_and_clears_transient_fields():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(
            input_path='/media/cancel-queued.mkv',
            status='queued',
            eta_seconds=90,
            fps=12.3,
            error_message='old error',
            cancel_requested=True,
            completed_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        updated = cancel_job(db, job.id)

        assert updated is not None
        assert updated.status == 'queued'
        assert updated.error_message is None
        assert updated.eta_seconds is None
        assert updated.fps is None
        assert updated.cancel_requested is False
        assert updated.completed_at is None


def test_cancel_job_stops_running_ffmpeg_and_requeues(monkeypatch):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(input_path='/media/cancel-running.mkv', status='running', cancel_requested=True)
        db.add(job)
        db.commit()
        db.refresh(job)

        stopped: list[int] = []
        monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped.append(job_id))

        updated = cancel_job(db, job.id)

        assert updated is not None
        assert updated.status == 'queued'
        assert updated.cancel_requested is False
        assert stopped == [job.id]


def test_resume_job_requeues_paused_job_without_clearing_resume_state():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(
            input_path='/media/resume-me.mkv',
            status='paused',
            progress_percent=57,
            resume_position_seconds=95.0,
            fps=10.0,
            eta_seconds=300,
            error_message='temporary',
            completed_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        updated = resume_job(db, job.id)

        assert updated is not None
        assert updated.status == 'queued'
        assert updated.progress_percent == 57
        assert updated.resume_position_seconds == 95.0
        assert updated.fps is None
        assert updated.eta_seconds is None
        assert updated.error_message is None
        assert updated.completed_at is None


def test_retry_job_recovers_resume_position_from_partial_workspace(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(workspace_root=str(tmp_path / 'workspaces'))
        db.add(settings)
        db.commit()

        job = Job(input_path='/media/retry-me.mkv', status='cancelled', progress_percent=61)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = tmp_path / 'workspaces' / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr('app.services.job_service._probe_partial_duration', lambda *_: 142.5)

        updated = retry_job(db, job.id)

        assert updated is not None
        assert updated.status == 'queued'
        assert updated.progress_percent == 61
        assert updated.resume_position_seconds == 142.5


def test_retry_job_resets_progress_when_no_partial_workspace_resume_state(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(workspace_root=str(tmp_path / 'workspaces'))
        db.add(settings)
        db.commit()

        job = Job(input_path='/media/retry-clean.mkv', status='failed', progress_percent=47)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = tmp_path / 'workspaces' / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr('app.services.job_service._probe_partial_duration', lambda *_: None)

        updated = retry_job(db, job.id)

        assert updated is not None
        assert updated.status == 'queued'
        assert updated.progress_percent == 0
        assert updated.resume_position_seconds is None


def test_refresh_queued_job_snapshots_updates_queued_only():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, codec='h264', speed_preset='medium')
        db.add(profile)
        db.commit()
        db.refresh(profile)

        queued_job = create_job(db, '/media/movies/a.mkv', library_id=library.id, profile=profile)
        running_job = Job(input_path='/media/movies/b.mkv', status='running', library_id=library.id,
                          profile_snapshot_json=queued_job.profile_snapshot_json)
        db.add(running_job)
        db.commit()

        # Update the profile to 'fast'
        profile.speed_preset = 'fast'
        db.commit()
        db.refresh(profile)

        count = refresh_queued_job_snapshots(db, library.id, profile)

        db.refresh(queued_job)
        db.refresh(running_job)

        assert count == 1
        assert '"speed_preset": "fast"' in queued_job.profile_snapshot_json
        # Running job must not be touched
        assert '"speed_preset": "medium"' in running_job.profile_snapshot_json

        db.delete(queued_job)
        db.delete(running_job)
        db.delete(profile)
        db.delete(library)
        db.commit()
