from datetime import UTC, datetime, timedelta

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.models.settings import Settings
from app.services import optimization_service
from app.services.job_service import abort_job, cancel_job, create_job, delete_job, job_exists_for_source, pause_job, prune_job_history, refresh_queued_job_snapshots, resume_job, retry_job


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


def test_delete_job_stops_active_encoder_and_removes_workspace(monkeypatch, tmp_path):
    stopped: list[int] = []
    deleted_workspaces: list[int] = []
    monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped.append(job_id) or True)
    monkeypatch.setattr(optimization_service, 'delete_workspace', lambda _settings, job_id: deleted_workspaces.append(job_id))

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(workspace_root=str(tmp_path / 'workspaces'))
        job = Job(input_path='/media/active-delete.mkv', status='running', progress_percent=22)
        db.add_all([settings, job])
        db.commit()
        job_id = job.id

        assert delete_job(db, job_id) is True
        assert db.query(Job).filter(Job.id == job_id).first() is None

    assert stopped == [job_id]
    assert deleted_workspaces == [job_id]


def test_job_exists_for_source_ignores_stale_complete_without_output(tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(
            input_path='/media/stale-complete.mkv',
            library_id=1,
            status='complete',
            output_path=str(tmp_path / 'missing-output.mkv'),
        )
        db.add(job)
        db.commit()

        assert job_exists_for_source(db, '/media/stale-complete.mkv', library_id=1) is False


def test_job_exists_for_source_matches_existing_job_even_when_library_differs():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        job = Job(
            input_path='/media/shared-source.mkv',
            library_id=None,
            status='queued',
        )
        db.add(job)
        db.commit()

        assert job_exists_for_source(db, '/media/shared-source.mkv', library_id=7) is True


def test_create_job_reuses_existing_source_and_enriches_missing_metadata():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, codec='hevc', output_suffix='-opt')
        db.add(profile)
        db.commit()
        db.refresh(profile)

        existing = Job(
            input_path='/media/movies/shared-title.mkv',
            status='queued',
            library_id=None,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)

        reused = create_job(
            db,
            '/media/movies/shared-title.mkv',
            library_id=library.id,
            profile=profile,
            source_resolution=2160,
            source_is_hdr=True,
        )

        db.refresh(existing)

        assert reused.id == existing.id
        assert existing.library_id == library.id
        assert existing.profile_snapshot_json is not None
        assert existing.source_resolution == 2160
        assert existing.source_is_hdr is True
        assert db.query(Job).filter(Job.input_path == '/media/movies/shared-title.mkv').count() == 1


def test_create_job_reuses_active_movie_identity_for_upgraded_release_filename():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        existing = Job(
            input_path='/media/movies/Example Movie (2026)/Example Movie (2026) [WEBDL-2160p x265].mkv',
            status='queued',
            library_id=library.id,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)

        reused = create_job(
            db,
            '/media/movies/Example Movie (2026)/Example Movie (2026) [Bluray-2160p Remux].mkv',
            library_id=library.id,
        )

        assert reused.id == existing.id
        assert db.query(Job).filter(Job.library_id == library.id).count() == 1


def test_job_exists_for_source_dedupes_active_tv_episode_upgrade_by_identity():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='TV', path='/media/tv', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        existing = Job(
            input_path='/media/tv/Example Show/Season 02/Example Show - S02E03 - Old HDTV.mkv',
            status='running',
            library_id=library.id,
        )
        db.add(existing)
        db.commit()

        assert job_exists_for_source(
            db,
            '/media/tv/Example Show/Season 02/Example Show - S02E03 - New WEB-DL.mkv',
            library_id=library.id,
        ) is True


def test_job_exists_for_source_does_not_block_upgrade_from_completed_history(tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        output_path = tmp_path / 'Example Movie (2026)-1080p.mkv'
        output_path.write_text('encoded')
        db.add(
            Job(
                input_path='/media/movies/Example Movie (2026)/Example Movie (2026) [WEBDL-2160p x265].mkv',
                status='complete',
                output_path=str(output_path),
                library_id=library.id,
            )
        )
        db.commit()

        assert job_exists_for_source(
            db,
            '/media/movies/Example Movie (2026)/Example Movie (2026) [Bluray-2160p Remux].mkv',
            library_id=library.id,
        ) is False


def test_prune_job_history_removes_stale_terminal_jobs():
    with SessionLocal() as db:
        stale_terminal = Job(
            input_path='/media/old.mkv',
            status='complete',
            completed_at=datetime.now(UTC) - timedelta(days=40),
        )
        fresh_terminal = Job(
            input_path='/media/new.mkv',
            status='failed',
            completed_at=datetime.now(UTC) - timedelta(days=1),
        )
        stale_active = Job(
            input_path='/media/running.mkv',
            status='running',
            completed_at=datetime.now(UTC) - timedelta(days=40),
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
            completed_at=datetime.now(UTC),
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


def test_abort_job_marks_running_job_aborting_and_requests_cancel(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(workspace_root=str(tmp_path / 'workspaces'))
        db.add(settings)
        db.commit()

        job = Job(input_path='/media/abort-running.mkv', status='running', cancel_requested=False)
        db.add(job)
        db.commit()
        db.refresh(job)

        stopped: list[int] = []
        deleted_workspaces: list[int] = []
        monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped.append(job_id))
        monkeypatch.setattr(optimization_service, 'delete_workspace', lambda _settings, job_id: deleted_workspaces.append(job_id))

        updated = abort_job(db, job.id)

        assert updated is not None
        assert updated.status == 'aborting'
        assert updated.cancel_requested is True
        assert updated.completed_at is None
        assert stopped == [job.id]
        assert deleted_workspaces == []


def test_abort_job_cancels_queued_job_immediately(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(workspace_root=str(tmp_path / 'workspaces'))
        db.add(settings)
        db.commit()

        job = Job(input_path='/media/abort-queued.mkv', status='queued', cancel_requested=False, progress_percent=37)
        db.add(job)
        db.commit()
        db.refresh(job)

        stopped: list[int] = []
        deleted_workspaces: list[int] = []
        monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped.append(job_id))
        monkeypatch.setattr(optimization_service, 'delete_workspace', lambda _settings, job_id: deleted_workspaces.append(job_id))

        updated = abort_job(db, job.id)

        assert updated is not None
        assert updated.status == 'cancelled'
        assert updated.error_message == 'Aborted by user'
        assert updated.cancel_requested is False
        assert updated.completed_at is not None
        assert updated.progress_percent == 0
        assert stopped == [job.id]
        assert deleted_workspaces == [job.id]


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
            completed_at=datetime.now(UTC),
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
