from datetime import datetime
import json
import shutil
import time

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.job import Job
from app.models.library import Library, LibraryProfile, SchedulePolicyEnum
from app.models.settings import Settings
from app.services.optimization_service import OptimizationMetrics
from app.services import notification_service
from app.workers import queue


def _wait_for_terminal_status(client: TestClient, job_id: int, timeout: float = 3.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        response = client.get(f'/jobs/{job_id}')
        payload = response.json()
        if payload['status'] in {'complete', 'failed', 'skipped', 'cancelled'}:
            return payload
        time.sleep(0.05)
    return client.get(f'/jobs/{job_id}').json()


def test_worker_retries_a_failed_job_once(monkeypatch):
    queue.resume_queue()
    target_input = '/media/test.mkv'
    call_count = {'count': 0}

    def always_fail(*args, **kwargs):
        if args and args[0] == target_input:
            call_count['count'] += 1
        return OptimizationMetrics(
            input_path='/media/test.mkv',
            output_path='/media/test-1080p.mkv',
            status='failed',
            skipped_reason='forced_failure',
        )

    monkeypatch.setattr(queue, 'optimize_video', always_fail)
    monkeypatch.setattr(queue, 'preflight_job', lambda *_: True)
    failure_notifications = []
    terminal_updates = []
    monkeypatch.setattr(queue, 'enqueue_job_failed', lambda job: failure_notifications.append(job.id))
    monkeypatch.setattr(queue, 'handle_job_terminal_state', lambda job_id, status: terminal_updates.append((job_id, status)))

    with SessionLocal() as session:
        session.query(Job).delete()
        session.commit()

    with TestClient(app) as client:
        settings_response = client.post('/settings', json={'enable_optimizer': True, 'global_quiet_enabled': False})
        assert settings_response.status_code == 200

        create_response = client.post('/jobs', json={'source_path': target_input})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        payload = _wait_for_terminal_status(client, job_id)

    assert payload['status'] == 'failed'
    assert payload['retry_count'] == 1
    assert call_count['count'] == 2
    assert failure_notifications == [job_id]
    assert terminal_updates[-1] == (job_id, 'failed')




def test_worker_marks_job_failed_when_unhandled_exception_occurs(monkeypatch):
    queue.resume_queue()

    def raise_unhandled(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(queue, 'optimize_video', raise_unhandled)
    monkeypatch.setattr(queue, 'preflight_job', lambda *_: True)

    with SessionLocal() as session:
        session.query(Job).delete()
        session.query(Settings).delete()
        session.add(Settings(enable_optimizer=True, max_workers=1, global_quiet_enabled=False))
        session.commit()

    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/test-crash.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        payload = _wait_for_terminal_status(client, job_id)

    assert payload['status'] == 'failed'
    assert payload['error_message'] == 'boom'


def test_should_workers_run_honors_manual_pause_state():
    settings = queue.Settings(enable_optimizer=True, global_quiet_enabled=True)

    queue.resume_queue()
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is True

    settings.enable_optimizer = False
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is False

    settings.enable_optimizer = True
    queue.pause_queue()
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is False

    queue.resume_queue()


def test_library_job_can_start_honors_library_schedule_and_enablement():
    settings = queue.Settings(enable_optimizer=True, global_quiet_enabled=False)
    library = queue.Library(enabled=True)
    profile = queue.LibraryProfile(schedule_enabled=True, schedule_start_hour=22, schedule_end_hour=6)

    assert queue._library_job_can_start(settings, datetime(2024, 1, 1, 23, 0, 0), library, profile) is True
    assert queue._library_job_can_start(settings, datetime(2024, 1, 1, 12, 0, 0), library, profile) is False

    profile.schedule_enabled = False
    assert queue._library_job_can_start(settings, datetime(2024, 1, 1, 12, 0, 0), library, profile) is True

    library.enabled = False
    assert queue._library_job_can_start(settings, datetime(2024, 1, 1, 23, 0, 0), library, profile) is False


def test_should_cancel_when_shutdown_requested():
    queue.stop_event.set()
    try:
        with SessionLocal() as db:
            assert queue._should_cancel(db, 999999) is True
    finally:
        queue.stop_event.clear()


def test_batch_tracking_enqueues_completion_email(monkeypatch):
    sent = []
    monkeypatch.setattr(notification_service, 'enqueue_email', lambda subject, body: sent.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_batch_complete = True
        db.commit()

    notification_service.register_scan_batch([301, 302])
    notification_service.handle_job_terminal_state(301, 'complete')
    assert sent == []

    notification_service.handle_job_terminal_state(302, 'failed')
    assert len(sent) == 1
    assert sent[0][0] == 'Optimizarr batch complete'
    assert 'Processed: 2' in sent[0][1]
    assert 'Failed: 1' in sent[0][1]


def test_preflight_job_skips_when_input_missing(tmp_path):
    job = Job(input_path=str(tmp_path / 'missing.mkv'), status='queued')

    passed = queue.preflight_job(job, queue.Settings())

    assert passed is False
    assert job.status == 'skipped'
    assert job.error_message == 'Input missing'


def test_preflight_job_skips_when_criteria_no_longer_matches(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'hdr_only': True, 'output_suffix': '-opt', 'container': 'mkv'}),
    )

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(queue, 'is_hdr_video', lambda _: False)

    passed = queue.preflight_job(job, queue.Settings())

    assert passed is False
    assert job.status == 'skipped'
    assert job.error_message == 'No longer matches criteria'


def test_preflight_job_uses_profile_minimum_source_resolution(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({
            'hdr_only': False,
            'minimum_source_resolution': 1080,
            'target_resolution': 720,
            'output_suffix': '-opt',
            'container': 'mkv',
        }),
    )

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 1080)
    monkeypatch.setattr(queue, '_cache_free_bytes', lambda _: 100 * queue.BYTES_PER_GB)

    passed = queue.preflight_job(job, queue.Settings(min_free_gb=25))

    assert passed is True
    assert job.status == 'queued'


def test_preflight_job_sets_output_from_snapshot_and_checks_cache(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'hdr_only': False, 'output_suffix': '-opt', 'container': 'mp4'}),
    )

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    settings = queue.Settings(min_free_gb=25)
    monkeypatch.setattr(queue, '_cache_free_bytes', lambda _: (settings.min_free_gb * queue.BYTES_PER_GB) - 1)
    monkeypatch.setattr(queue, 'enqueue_low_disk_space_alert', lambda **_: None)
    monkeypatch.setattr(queue.broker, 'publish_notification', lambda *_: None)

    passed = queue.preflight_job(job, settings)

    assert passed is False
    assert job.output_path.endswith('-opt.mp4')
    assert job.status == 'failed'
    assert job.error_message == 'Insufficient cache space'


def test_preflight_job_applies_output_conflict_policy_rename(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    existing = tmp_path / 'movie-opt.mkv'
    existing.write_text('output')

    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'output_suffix': '-opt', 'container': 'mkv', 'output_conflict_policy': 'rename'}),
    )

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(queue, '_cache_free_bytes', lambda _: 100 * queue.BYTES_PER_GB)

    passed = queue.preflight_job(job, queue.Settings(min_free_gb=25))

    assert passed is True
    assert job.output_path.endswith('-opt-v2.mkv')


def test_preflight_job_low_disk_pauses_queue_and_alerts(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'output_suffix': '-opt', 'container': 'mkv'}),
    )

    settings = queue.Settings(min_free_gb=25)
    alerts = []
    notifications = []
    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(queue, '_cache_free_bytes', lambda _: (settings.min_free_gb * queue.BYTES_PER_GB) - 1)
    monkeypatch.setattr(queue, 'enqueue_low_disk_space_alert', lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(queue.broker, 'publish_notification', lambda event: notifications.append(event))

    queue.resume_queue()
    queue._clear_low_disk_alert()
    passed = queue.preflight_job(job, settings)

    assert passed is False
    assert job.status == 'failed'
    assert queue.is_queue_paused() is True
    assert alerts and alerts[0]['min_free_gb'] == 25
    assert notifications == ['queue_paused_low_disk']

    queue.resume_queue()
    queue._clear_low_disk_alert()


def test_cache_free_bytes_uses_workspace_root_parent_when_path_missing(monkeypatch, tmp_path):
    workspace_parent = tmp_path / 'workspace-mount'
    workspace_parent.mkdir()
    workspace_root = workspace_parent / 'nested' / 'child'
    settings = queue.Settings(workspace_root=str(workspace_root))

    called = []

    def fake_disk_usage(path):
        called.append(str(path))
        return shutil._ntuple_diskusage(total=100, used=10, free=90)

    monkeypatch.setattr(queue.shutil, 'disk_usage', fake_disk_usage)

    free_bytes = queue._cache_free_bytes(settings)

    assert free_bytes == 90
    assert called == [str(workspace_parent)]


def test_preflight_job_low_space_uses_workspace_root_path(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    workspace_root = tmp_path / 'workspaces'
    workspace_root.mkdir()
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'output_suffix': '-opt', 'container': 'mkv'}),
    )

    settings = queue.Settings(min_free_gb=25, workspace_root=str(workspace_root))
    checked_paths = []

    def fake_disk_usage(path):
        checked_paths.append(str(path))
        if str(path) == str(workspace_root):
            return shutil._ntuple_diskusage(total=100, used=90, free=(settings.min_free_gb * queue.BYTES_PER_GB) - 1)
        return shutil._ntuple_diskusage(total=100, used=10, free=100 * queue.BYTES_PER_GB)

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(queue.shutil, 'disk_usage', fake_disk_usage)
    monkeypatch.setattr(queue, 'enqueue_low_disk_space_alert', lambda **_: None)
    monkeypatch.setattr(queue.broker, 'publish_notification', lambda *_: None)

    passed = queue.preflight_job(job, settings)

    assert passed is False
    assert job.status == 'failed'
    assert str(workspace_root) in checked_paths


def test_preflight_job_does_not_fail_when_root_low_and_workspace_has_capacity(monkeypatch, tmp_path):
    media = tmp_path / 'movie.mkv'
    media.write_text('x')
    workspace_root = tmp_path / 'workspaces'
    workspace_root.mkdir()
    job = Job(
        input_path=str(media),
        status='queued',
        profile_snapshot_json=json.dumps({'output_suffix': '-opt', 'container': 'mkv'}),
    )

    settings = queue.Settings(min_free_gb=25, workspace_root=str(workspace_root))

    def fake_disk_usage(path):
        if str(path) == '/':
            return shutil._ntuple_diskusage(total=100, used=99, free=1)
        if str(path) == str(workspace_root):
            return shutil._ntuple_diskusage(total=100, used=10, free=100 * queue.BYTES_PER_GB)
        raise AssertionError(f'unexpected path checked: {path}')

    monkeypatch.setattr(queue, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(queue.shutil, 'disk_usage', fake_disk_usage)

    passed = queue.preflight_job(job, settings)

    assert passed is True
    assert job.status == 'queued'


def test_should_workers_run_honors_manual_pause():
    settings = queue.Settings(enable_optimizer=True, global_quiet_enabled=False)

    queue.pause_queue()
    try:
        assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is False
    finally:
        queue.resume_queue()

    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is True


def test_enforce_schedule_policy_pauses_running_job(monkeypatch):
    stopped = []
    monkeypatch.setattr(queue, 'stop_active_ffmpeg', lambda job_id: stopped.append(job_id))

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(enable_optimizer=True, global_quiet_enabled=False)
        db.add(settings)
        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(
            library_id=library.id,
            schedule_start_hour=1,
            schedule_end_hour=2,
            schedule_policy=SchedulePolicyEnum.pause_current,
        )
        db.add(profile)
        db.commit()

        job = Job(input_path='/media/movies/a.mkv', status='running', library_id=library.id)
        db.add(job)
        db.commit()
        db.refresh(job)

        queue._enforce_library_schedule_policies(db, settings, datetime(2024, 1, 1, 12, 0, 0))

        db.refresh(job)
        assert job.status == 'paused_schedule'
        assert stopped == [job.id]


def test_enforce_schedule_policy_requeues_paused_schedule_job_when_window_opens(monkeypatch):
    deleted_partials: list = []
    # Partial outputs are preserved on schedule-resume so the job can continue
    # from where it left off; the monkeypatch records any unexpected deletions.

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(enable_optimizer=True, global_quiet_enabled=False)
        db.add(settings)
        library = Library(name='Shows', path='/media/shows', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(
            library_id=library.id,
            schedule_start_hour=8,
            schedule_end_hour=20,
            schedule_policy=SchedulePolicyEnum.pause_current,
        )
        db.add(profile)
        db.commit()

        job = Job(input_path='/media/shows/e1.mkv', status='paused_schedule', library_id=library.id, progress_percent=37)
        db.add(job)
        db.commit()
        db.refresh(job)

        queue._enforce_library_schedule_policies(db, settings, datetime(2024, 1, 1, 10, 0, 0))

        db.refresh(job)
        assert job.status == 'queued'
        # Progress is preserved so the UI shows existing progress during resume.
        assert job.progress_percent == 37
        # Partial output is NOT deleted on schedule-resume; it will be used for a
        # seek-based resume instead of re-encoding from scratch.
        assert deleted_partials == []


def test_pause_and_resume_queue_publish_system_events(monkeypatch):
    events = []
    monkeypatch.setattr(queue.broker, 'publish_system_event', lambda event, **data: events.append((event, data)))

    queue.resume_queue(reason='manual')
    queue.pause_queue(reason='manual')
    queue.resume_queue(reason='manual')

    assert events[0] == ('queue_paused', {'reason': 'manual'})
    assert events[1] == ('queue_resumed', {'reason': 'manual'})


def test_enforce_schedule_policy_publishes_state_change(monkeypatch):
    events = []
    monkeypatch.setattr(queue, 'stop_active_ffmpeg', lambda *_: None)
    monkeypatch.setattr(queue.broker, 'publish_system_event', lambda event, **data: events.append((event, data)))

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(enable_optimizer=True, global_quiet_enabled=False)
        db.add(settings)
        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(
            library_id=library.id,
            schedule_start_hour=1,
            schedule_end_hour=2,
            schedule_policy=SchedulePolicyEnum.pause_current,
        )
        db.add(profile)
        db.commit()

        job = Job(input_path='/media/movies/a.mkv', status='running', library_id=library.id)
        db.add(job)
        db.commit()

        queue._enforce_library_schedule_policies(db, settings, datetime(2024, 1, 1, 12, 0, 0))

    assert events
    assert events[0][0] == 'schedule_policy_state_changed'
    assert events[0][1]['state'] == 'paused_schedule'


def test_start_queued_job_manual_bypasses_optimizer_and_schedule(monkeypatch):
    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(queue, 'Thread', DummyThread)
    monkeypatch.setattr(queue, '_library_job_can_start', lambda *_: False)

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(enable_optimizer=False, max_workers=1)
        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add_all([settings, library])
        db.commit()
        db.refresh(library)

        job = Job(input_path='/media/movies/a.mkv', status='queued', library_id=library.id)
        db.add(job)
        db.commit()
        db.refresh(job)

        started, reason = queue.start_queued_job(job.id, manual=True)

        db.refresh(job)
        assert started is True
        assert reason is None
        assert job.status == 'starting'

    queue._active_workers.clear()


def test_start_queued_job_non_manual_rejects_when_optimizer_disabled():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(Settings).delete()
        db.commit()

        settings = Settings(enable_optimizer=False, max_workers=1)
        db.add(settings)
        db.commit()

        job = Job(input_path='/media/movies/a.mkv', status='queued')
        db.add(job)
        db.commit()
        db.refresh(job)

        started, reason = queue.start_queued_job(job.id)

        assert started is False
        assert reason == 'Optimizer is disabled'
