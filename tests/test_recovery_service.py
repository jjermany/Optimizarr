import json
from datetime import UTC, datetime, timedelta
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


def test_startup_recovery_caps_encode_runtime_at_last_activity(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'

    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, workspace_root, requeue=True)

        started_at = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
        last_activity_at = started_at + timedelta(seconds=45)
        job = Job(
            input_path='/media/restart-runtime.mkv',
            status='running',
            progress_percent=61,
            encode_started_at=started_at,
            last_encode_activity_at=last_activity_at,
            encode_duration_seconds=12,
        )
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
        assert job.encode_duration_seconds == 57
        assert job.encode_started_at is None
        assert job.last_encode_activity_at is None






def test_startup_recovery_requeues_paused_jobs(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, tmp_path / 'workspaces', requeue=True)

        job = Job(input_path='/media/restart-paused.mkv', status='paused', progress_percent=47)
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



def test_startup_recovery_pins_resumed_job_to_last_working_encoder(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'

    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, workspace_root, requeue=True)

        snapshot = {
            'codec': 'av1',
            'preferred_video_encoder': 'auto',
            'container': 'mkv',
            'target_resolution': 1080,
        }
        job = Job(
            input_path='/media/resume-encoder.mkv',
            status='running',
            progress_percent=50,
            profile_snapshot_json=json.dumps(snapshot),
            codec_used='hevc',
            encoder_used='hevc_vaapi',
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 77.0)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(job)

        profile_snapshot = json.loads(job.profile_snapshot_json or '{}')
        assert summary['recovered_jobs'] == 1
        assert summary['requeued_jobs'] == 1
        assert job.resume_position_seconds == 77.0
        assert profile_snapshot['codec'] == 'hevc'
        assert profile_snapshot['preferred_video_encoder'] == 'hevc_vaapi'

def test_startup_recovery_adds_resume_position_for_queued_partials(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'

    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, workspace_root, requeue=True)

        job = Job(input_path='/media/queued-partial.mkv', status='queued', progress_percent=33)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 88.8)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(job)

        assert summary['recovered_jobs'] == 0
        assert summary['requeued_jobs'] == 1
        assert job.status == 'queued'
        assert job.resume_position_seconds == 88.8
        assert job.progress_percent == 33


def test_startup_recovery_normalizes_legacy_pending_and_created_to_queued(tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, tmp_path / 'workspaces', requeue=True)

        pending_job = Job(input_path='/media/legacy-pending.mkv', status='pending', progress_percent=91, retry_count=4)
        created_job = Job(input_path='/media/legacy-created.mkv', status='created', progress_percent=8, retry_count=1)
        db.add_all([pending_job, created_job])
        db.commit()
        db.refresh(pending_job)
        db.refresh(created_job)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(pending_job)
        db.refresh(created_job)

        assert summary['recovered_jobs'] == 0
        assert summary['requeued_jobs'] == 2
        assert pending_job.status == 'queued'
        assert created_job.status == 'queued'
        assert pending_job.progress_percent == 0
        assert created_job.progress_percent == 0
        assert pending_job.retry_count == 0
        assert created_job.retry_count == 0
        assert pending_job.cancel_requested is False
        assert created_job.cancel_requested is False


def test_startup_recovery_normalized_legacy_pending_keeps_resume_capability(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'

    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()

        _configure_settings(db, workspace_root, requeue=True)

        job = Job(input_path='/media/legacy-pending-partial.mkv', status='pending', progress_percent=71)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 66.6)

        summary = recovery_service.run_startup_recovery(db)
        db.refresh(job)

        assert summary['recovered_jobs'] == 0
        assert summary['requeued_jobs'] == 2
        assert job.status == 'queued'
        assert job.resume_position_seconds == 66.6
        assert workspace.exists()

# ---------------------------------------------------------------------------
# run_workspace_cleanup edge cases
# ---------------------------------------------------------------------------

def test_workspace_cleanup_preserves_queued_job_with_resume_position(monkeypatch, tmp_path):
    """A queued job with resume_position_seconds must keep its workspace (partial inside)."""
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        job = Job(input_path='/media/resume-me.mkv', status='queued', resume_position_seconds=77.5)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')
        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 77.5)

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 0
        assert job.id not in summary['cleaned_workspace_job_ids']
        assert workspace.exists(), 'Workspace must survive for the resume to work'


def test_workspace_cleanup_removes_stale_ffmpeg_artifacts_from_resumable_workspace(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        job = Job(input_path='/media/resume-clean.mkv', status='queued', resume_position_seconds=80.0)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        partial = workspace / 'output.partial.mkv'
        partial.write_text('partial')
        stale_resume = workspace / 'output.resume.mkv'
        stale_resume.write_text('resume')
        stale_combined = workspace / 'output.combined.mkv'
        stale_combined.write_text('combined')
        stale_recovered = workspace / 'output.partial.recovered.mkv'
        stale_recovered.write_text('recovered')
        concat_list = workspace / 'concat_list.txt'
        concat_list.write_text('file output.partial.mkv')
        keep_marker = workspace / 'keep.txt'
        keep_marker.write_text('keep')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 80.0)

        summary = recovery_service.run_workspace_cleanup(db)
        db.refresh(job)

        assert summary['cleaned_workspaces'] == 0
        assert summary['cleaned_workspace_artifacts'] == 4
        assert partial.exists()
        assert keep_marker.exists()
        assert not stale_resume.exists()
        assert not stale_combined.exists()
        assert not stale_recovered.exists()
        assert not concat_list.exists()
        assert job.resume_position_seconds == 80.0


def test_workspace_cleanup_removes_broken_resumable_workspace(monkeypatch, tmp_path):
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        job = Job(input_path='/media/broken-resume.mkv', status='queued', resume_position_seconds=80.0, progress_percent=45)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('broken')
        (workspace / 'output.resume.mkv').write_text('stale')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: None)

        summary = recovery_service.run_workspace_cleanup(db)
        db.refresh(job)

        assert summary['cleaned_workspaces'] == 1
        assert summary['cleaned_workspace_job_ids'] == [job.id]
        assert not workspace.exists()
        assert job.resume_position_seconds is None
        assert job.progress_percent == 0


def test_workspace_cleanup_removes_queued_job_workspace_without_resume_position(tmp_path):
    """A queued job without resume_position_seconds has no valid partial; clean it up."""
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        job = Job(input_path='/media/no-resume.mkv', status='queued', resume_position_seconds=None)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'stale_output.mkv').write_text('stale')

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 1
        assert job.id in summary['cleaned_workspace_job_ids']
        assert not workspace.exists()


def test_workspace_cleanup_removes_terminal_job_workspace_even_with_resume_position(tmp_path):
    """A completed/failed job should never keep a workspace regardless of resume_position_seconds."""
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        # Simulate an unlikely data inconsistency: terminal job still has a resume position.
        job = Job(input_path='/media/done.mkv', status='complete', resume_position_seconds=50.0)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('stale')

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 1
        assert not workspace.exists()


def test_workspace_cleanup_preserves_active_status_workspaces(tmp_path):
    """Jobs in ACTIVE_WORKSPACE_STATUSES always keep their workspaces."""
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        active_jobs = []
        for status in ('running', 'preflight', 'starting', 'aborting', 'paused', 'paused_schedule'):
            job = Job(input_path=f'/media/{status}.mkv', status=status)
            db.add(job)
            db.commit()
            db.refresh(job)
            workspace = workspace_root / str(job.id)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / 'output.partial.mkv').write_text('active')
            active_jobs.append(job)

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 0
        for job in active_jobs:
            assert (workspace_root / str(job.id)).exists(), f'Workspace for {job.status} job must survive'


def test_workspace_cleanup_ignores_non_numeric_directories(tmp_path):
    """Non-numeric directories under the workspace root are left untouched."""
    workspace_root = tmp_path / 'workspaces'
    workspace_root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        oddball = workspace_root / 'lost+found'
        oddball.mkdir()
        (oddball / 'junk').write_text('junk')

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 0
        assert oddball.exists()


def test_workspace_cleanup_removes_orphaned_workspace_for_unknown_job_id(tmp_path):
    """A workspace whose job ID does not exist in the DB is cleaned up."""
    workspace_root = tmp_path / 'workspaces'
    workspace_root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root)

        orphan = workspace_root / '99999'
        orphan.mkdir()
        (orphan / 'output.partial.mkv').write_text('orphan')

        summary = recovery_service.run_workspace_cleanup(db)

        assert summary['cleaned_workspaces'] == 1
        assert not orphan.exists()


# ---------------------------------------------------------------------------
# End-to-end: startup recovery → workspace cleanup (the original bug scenario)
# ---------------------------------------------------------------------------

def test_startup_recovery_then_cleanup_preserves_partial_for_resume(monkeypatch, tmp_path):
    """
    Full restart sequence: recovery re-queues an interrupted job that has a
    valid partial; the subsequent workspace cleanup must NOT delete it.

    This is the core regression test for the bug where run_workspace_cleanup
    immediately wiped the partial that run_startup_recovery had just preserved.
    """
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root, requeue=True)

        job = Job(input_path='/media/interrupted.mkv', status='running', progress_percent=42)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial-data')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: 99.9)

        recovery_summary = recovery_service.run_startup_recovery(db)
        cleanup_summary = recovery_service.run_workspace_cleanup(db)

        db.refresh(job)
        assert job.status == 'queued'
        assert job.resume_position_seconds == 99.9
        assert workspace.exists(), 'Partial workspace must survive cleanup so the job can resume'
        assert cleanup_summary['cleaned_workspaces'] == 0
        assert job.id not in cleanup_summary['cleaned_workspace_job_ids']
        # Recovery counters
        assert recovery_summary['recovered_jobs'] == 1
        assert recovery_summary['requeued_jobs'] == 1
        assert recovery_summary['cleaned_workspaces'] == 0


def test_startup_recovery_then_cleanup_removes_workspace_when_no_partial(monkeypatch, tmp_path):
    """
    When there is no usable partial (ffprobe returns None), recovery resets the
    job to queued-from-scratch and deletes the workspace itself.  The subsequent
    cleanup should find nothing left to do.
    """
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root, requeue=True)

        job = Job(input_path='/media/no-partial.mkv', status='running', progress_percent=10)
        db.add(job)
        db.commit()
        db.refresh(job)

        workspace = workspace_root / str(job.id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('broken')

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', lambda *_: None)

        recovery_service.run_startup_recovery(db)
        cleanup_summary = recovery_service.run_workspace_cleanup(db)

        db.refresh(job)
        assert job.status == 'queued'
        assert job.resume_position_seconds is None
        assert not workspace.exists()
        # Workspace was already cleaned during recovery; cleanup has nothing to do.
        assert cleanup_summary['cleaned_workspaces'] == 0


def test_startup_recovery_then_cleanup_mixed_jobs(monkeypatch, tmp_path):
    """
    Multiple interrupted jobs in a single restart:
    - one with a valid partial → workspace preserved
    - one without a partial → workspace cleaned
    Both are re-queued; only the one with the partial keeps its workspace.
    """
    workspace_root = tmp_path / 'workspaces'
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()
        _configure_settings(db, workspace_root, requeue=True)

        job_with = Job(input_path='/media/with-partial.mkv', status='running', progress_percent=55)
        job_without = Job(input_path='/media/without-partial.mkv', status='running', progress_percent=20)
        db.add(job_with)
        db.add(job_without)
        db.commit()
        db.refresh(job_with)
        db.refresh(job_without)

        ws_with = workspace_root / str(job_with.id)
        ws_with.mkdir(parents=True, exist_ok=True)
        (ws_with / 'output.partial.mkv').write_text('good-partial')

        ws_without = workspace_root / str(job_without.id)
        ws_without.mkdir(parents=True, exist_ok=True)
        (ws_without / 'output.partial.mkv').write_text('bad-partial')

        def fake_probe(workspace: 'Path') -> 'float | None':
            if workspace == ws_with:
                return 88.0
            return None

        monkeypatch.setattr(recovery_service, '_probe_partial_duration', fake_probe)

        recovery_service.run_startup_recovery(db)
        cleanup_summary = recovery_service.run_workspace_cleanup(db)

        db.refresh(job_with)
        db.refresh(job_without)

        assert job_with.status == 'queued'
        assert job_with.resume_position_seconds == 88.0
        assert ws_with.exists(), 'Workspace with valid partial must survive cleanup'

        assert job_without.status == 'queued'
        assert job_without.resume_position_seconds is None
        assert not ws_without.exists()

        # Only the workspace that was already cleaned by recovery counts as cleaned there;
        # the post-recovery cleanup should see zero additional workspaces to remove.
        assert cleanup_summary['cleaned_workspaces'] == 0
