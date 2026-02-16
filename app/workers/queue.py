from __future__ import annotations

from datetime import datetime
import logging
from threading import Event, Lock, Thread
import time

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.settings import Settings
from app.services.job_service import prune_job_history
from app.services.notification_service import enqueue_job_failed, handle_job_terminal_state
from app.services.optimization_service import optimize_video
from app.services.realtime_service import broker

stop_event = Event()
_pool_lock = Lock()
_active_workers: dict[int, Thread] = {}
_manager_thread: Thread | None = None
_workers_allowed = True
_last_prune_at = 0.0

logger = logging.getLogger(__name__)


TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _should_cancel(db: Session, job_id: int) -> bool:
    if stop_event.is_set():
        return True
    job = db.query(Job).filter(Job.id == job_id).first()
    return bool(job and job.cancel_requested)


def _mark_finished(job: Job) -> None:
    if job.status in TERMINAL_STATUSES:
        job.completed_at = datetime.utcnow()


def _publish_job(job: Job, *, throttle_progress: bool = True) -> None:
    broker.publish_job_update(
        {
            'id': job.id,
            'status': job.status,
            'source_path': job.source_path,
            'output_path': job.output_path,
            'retry_count': job.retry_count,
            'cancel_requested': job.cancel_requested,
            'progress_percent': job.progress_percent,
            'fps': job.fps,
            'eta_seconds': job.eta_seconds,
            'encoder_used': job.encoder_used,
            'codec_used': job.codec_used,
            'hwaccel_used': job.hwaccel_used,
            'used_fallback': job.used_fallback,
            'fallback_reason': job.fallback_reason,
            'error_message': job.error_message,
        },
        throttle_progress=throttle_progress,
    )


def _process_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        if job.cancel_requested:
            job.status = 'cancelled'
            _mark_finished(job)
            db.commit()
            _publish_job(job, throttle_progress=False)
            return

        settings = _get_settings(db)
        job.status = 'running'
        job.progress_percent = 0
        job.error_message = None
        job.encoder_used = None
        job.codec_used = None
        job.hwaccel_used = None
        job.used_fallback = None
        job.fallback_reason = None
        db.commit()
        _publish_job(job, throttle_progress=False)

        def on_progress(update: dict[str, float | int | None]) -> None:
            db.refresh(job)
            if job.status in TERMINAL_STATUSES:
                return
            job.progress_percent = int(update.get('progress_percent') or 0)
            job.fps = update.get('fps') if isinstance(update.get('fps'), float) else None
            eta_seconds = update.get('eta_seconds')
            job.eta_seconds = int(eta_seconds) if isinstance(eta_seconds, int) else None
            db.commit()
            _publish_job(job, throttle_progress=True)

        metrics = optimize_video(
            job.input_path,
            settings,
            progress_callback=on_progress,
            should_cancel=lambda: _should_cancel(db, job_id),
        )

        db.refresh(job)
        job.status = metrics.status
        job.output_path = metrics.output_path
        job.fps = metrics.fps
        job.encoder_used = metrics.encoder_used
        job.codec_used = metrics.codec_used
        job.hwaccel_used = metrics.hwaccel_used
        job.used_fallback = metrics.used_fallback
        job.fallback_reason = metrics.fallback_reason
        if metrics.status == 'failed':
            job.error_message = metrics.error_message or metrics.skipped_reason or 'optimization_failed'
            if job.retry_count < 1:
                job.retry_count += 1
                job.status = 'queued'
                job.progress_percent = 0
                job.eta_seconds = None
                job.completed_at = None
            else:
                _mark_finished(job)
        elif metrics.status == 'complete':
            job.progress_percent = 100
            job.eta_seconds = 0
            _mark_finished(job)
        else:
            _mark_finished(job)
        db.commit()
        _publish_job(job, throttle_progress=False)
        if job.status == 'failed':
            enqueue_job_failed(job)
        handle_job_terminal_state(job.id, job.status)
    finally:
        db.close()
        with _pool_lock:
            _active_workers.pop(job_id, None)


def _claim_next_queued_job(db: Session) -> int | None:
    job = (
        db.query(Job)
        .filter(Job.status == 'queued')
        .order_by(Job.created_at.asc())
        .first()
    )
    if not job:
        return None

    job.status = 'starting'
    db.commit()
    _publish_job(job, throttle_progress=False)
    return job.id


def _is_within_schedule_window(current_hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour <= end_hour:
        return start_hour <= current_hour <= end_hour
    return current_hour >= start_hour or current_hour <= end_hour


def _should_workers_run(settings: Settings, now: datetime) -> bool:
    if not settings.enable_optimizer:
        return False
    return _is_within_schedule_window(now.hour, settings.schedule_start_hour, settings.schedule_end_hour)


def _manager_loop() -> None:
    global _workers_allowed
    global _last_prune_at
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            settings = _get_settings(db)
            _workers_allowed = _should_workers_run(settings, datetime.now())

            if time.monotonic() - _last_prune_at >= 60:
                deleted = prune_job_history(db, int(settings.history_retention_days))
                if deleted:
                    logger.info('Pruned %s stale completed jobs', deleted)
                _last_prune_at = time.monotonic()

            max_workers = max(1, int(settings.max_workers))

            if _workers_allowed:
                while True:
                    with _pool_lock:
                        active_count = len(_active_workers)
                    if active_count >= max_workers:
                        break

                    next_job_id = _claim_next_queued_job(db)
                    if not next_job_id:
                        break

                    worker = Thread(target=_process_job, args=(next_job_id,), daemon=True, name=f'optimizer-job-{next_job_id}')
                    worker.start()
                    with _pool_lock:
                        _active_workers[next_job_id] = worker
        finally:
            db.close()

        time.sleep(0.2)


def start_worker() -> Thread:
    global _manager_thread
    if _manager_thread and _manager_thread.is_alive():
        return _manager_thread

    stop_event.clear()
    _manager_thread = Thread(target=_manager_loop, name='optimizer-manager', daemon=True)
    _manager_thread.start()
    return _manager_thread


def stop_worker() -> None:
    global _manager_thread
    stop_event.set()

    db = SessionLocal()
    try:
        with _pool_lock:
            active_ids = list(_active_workers.keys())
        if active_ids:
            db.query(Job).filter(Job.id.in_(active_ids)).update({'cancel_requested': True}, synchronize_session=False)
            db.commit()
    finally:
        db.close()

    if _manager_thread and _manager_thread.is_alive():
        _manager_thread.join(timeout=5)

    with _pool_lock:
        workers = list(_active_workers.values())
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=5)

    _manager_thread = None
