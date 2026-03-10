from datetime import UTC, datetime, timedelta

from app.models.job import Job
from app.services.job_timing_service import start_encode_timing, stop_encode_timing, touch_encode_timing


def test_encode_timing_accumulates_across_pause_and_resume():
    job = Job(input_path='/media/example.mkv', status='running')
    start = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    start_encode_timing(job, now=start)
    touch_encode_timing(job, now=start + timedelta(seconds=25))
    stop_encode_timing(job, now=start + timedelta(seconds=30))

    resume_start = start + timedelta(minutes=5)
    start_encode_timing(job, now=resume_start)
    touch_encode_timing(job, now=resume_start + timedelta(seconds=15))
    stop_encode_timing(job, now=resume_start + timedelta(seconds=20))

    assert job.encode_duration_seconds == 50
    assert job.encode_started_at is None
    assert job.last_encode_activity_at is None


def test_encode_timing_recovery_uses_last_activity_not_restart_time():
    job = Job(input_path='/media/example.mkv', status='running')
    start = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    start_encode_timing(job, now=start)
    touch_encode_timing(job, now=start + timedelta(seconds=47))
    stop_encode_timing(
        job,
        now=start + timedelta(minutes=10),
        include_idle_since_last_activity=False,
    )

    assert job.encode_duration_seconds == 47
