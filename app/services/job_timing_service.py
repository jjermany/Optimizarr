from __future__ import annotations

from datetime import UTC, datetime

from app.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def start_encode_timing(job: Job, *, now: datetime | None = None) -> None:
    current = _as_utc(now) or _utcnow()
    job.encode_started_at = current
    job.last_encode_activity_at = current
    if job.encode_duration_seconds is None:
        job.encode_duration_seconds = 0


def touch_encode_timing(job: Job, *, now: datetime | None = None) -> None:
    if job.encode_started_at is None:
        return
    current = _as_utc(now) or _utcnow()
    if job.last_encode_activity_at is None or current >= _as_utc(job.last_encode_activity_at):
        job.last_encode_activity_at = current


def stop_encode_timing(
    job: Job,
    *,
    now: datetime | None = None,
    include_idle_since_last_activity: bool = True,
) -> int | None:
    started_at = _as_utc(job.encode_started_at)
    if started_at is None:
        job.last_encode_activity_at = None
        return job.encode_duration_seconds

    current = _as_utc(now) or _utcnow()
    last_activity_at = _as_utc(job.last_encode_activity_at)
    end_at = current if include_idle_since_last_activity else (last_activity_at or current)

    if last_activity_at is not None and include_idle_since_last_activity:
        end_at = max(end_at, last_activity_at)

    elapsed_seconds = max(0, int(round((end_at - started_at).total_seconds())))
    job.encode_duration_seconds = max(0, int(job.encode_duration_seconds or 0)) + elapsed_seconds
    job.encode_started_at = None
    job.last_encode_activity_at = None
    return job.encode_duration_seconds


def reset_encode_timing(job: Job, *, clear_duration: bool = False) -> None:
    job.encode_started_at = None
    job.last_encode_activity_at = None
    if clear_duration:
        job.encode_duration_seconds = None
