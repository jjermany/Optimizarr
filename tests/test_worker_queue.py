from datetime import datetime
import time

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.job import Job
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
    failure_notifications = []
    terminal_updates = []
    monkeypatch.setattr(queue, 'enqueue_job_failed', lambda job: failure_notifications.append(job.id))
    monkeypatch.setattr(queue, 'handle_job_terminal_state', lambda job_id, status: terminal_updates.append((job_id, status)))

    with SessionLocal() as session:
        session.query(Job).delete()
        session.commit()

    with TestClient(app) as client:
        settings_response = client.post('/settings', json={'enable_optimizer': True, 'schedule_start_hour': 0, 'schedule_end_hour': 23})
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


def test_should_workers_run_honors_enable_and_schedule():
    settings = queue.Settings(enable_optimizer=True, schedule_start_hour=9, schedule_end_hour=17)

    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is True
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 8, 0, 0)) is False

    settings.enable_optimizer = False
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 10, 0, 0)) is False


def test_should_workers_run_handles_overnight_windows():
    settings = queue.Settings(enable_optimizer=True, schedule_start_hour=22, schedule_end_hour=6)

    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 23, 0, 0)) is True
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 3, 0, 0)) is True
    assert queue._should_workers_run(settings, datetime(2024, 1, 1, 12, 0, 0)) is False


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
