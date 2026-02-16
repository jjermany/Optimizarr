from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
import logging
from pathlib import Path
from queue import Empty, Queue
import smtplib
from threading import Event, Lock, Thread

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library
from app.models.notification_settings import NotificationSettings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailEvent:
    subject: str
    body: str


@dataclass(slots=True)
class BatchTracker:
    pending_ids: set[int]
    library_name: str | None = None
    processed: int = 0
    failed: int = 0


_stop_event = Event()
_email_queue: Queue[EmailEvent] = Queue()
_worker_thread: Thread | None = None
_batch_lock = Lock()
_batches: list[BatchTracker] = []


def _emails_to_csv(emails: Iterable[str]) -> str:
    return ','.join(email.strip() for email in emails if email.strip())


def _emails_from_csv(csv_value: str) -> list[str]:
    return [email.strip() for email in csv_value.split(',') if email.strip()]


def _format_notification_body(*, library_name: str | None, file_name: str | None, reason: str, suggested_action: str) -> str:
    return (
        f'Library: {library_name or "unknown"}\n'
        f'File: {file_name or "n/a"}\n'
        f'Reason: {reason}\n'
        f'Suggested action: {suggested_action}\n'
    )


def get_or_create_notification_settings(db: Session) -> NotificationSettings:
    settings = db.query(NotificationSettings).first()
    if not settings:
        settings = NotificationSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def settings_to_payload(settings: NotificationSettings) -> dict:
    return {
        'smtp_host': settings.smtp_host,
        'smtp_port': settings.smtp_port,
        'smtp_user': settings.smtp_user,
        'smtp_password': settings.smtp_password,
        'smtp_tls': settings.smtp_tls,
        'from_email': settings.from_email,
        'to_emails': _emails_from_csv(settings.to_emails_csv),
        'notify_on': {
            'job_failed': settings.notify_on_job_failed,
            'job_interrupted': settings.notify_on_job_interrupted,
            'low_disk_pause': settings.notify_on_low_disk_pause,
            'recovery_ran': settings.notify_on_recovery_ran,
            'batch_complete': settings.notify_on_batch_complete,
        },
    }


def update_settings(db: Session, payload: dict) -> NotificationSettings:
    settings = get_or_create_notification_settings(db)

    for key in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_tls', 'from_email']:
        if key in payload:
            setattr(settings, key, payload[key])

    if 'to_emails' in payload:
        settings.to_emails_csv = _emails_to_csv(payload['to_emails'])

    notify_on = payload.get('notify_on') or {}
    if 'job_failed' in notify_on:
        settings.notify_on_job_failed = bool(notify_on['job_failed'])
    if 'job_interrupted' in notify_on:
        settings.notify_on_job_interrupted = bool(notify_on['job_interrupted'])
    if 'low_disk_pause' in notify_on:
        settings.notify_on_low_disk_pause = bool(notify_on['low_disk_pause'])
    if 'recovery_ran' in notify_on:
        settings.notify_on_recovery_ran = bool(notify_on['recovery_ran'])
    if 'batch_complete' in notify_on:
        settings.notify_on_batch_complete = bool(notify_on['batch_complete'])

    db.commit()
    db.refresh(settings)
    return settings


def _send_via_smtp(event: EmailEvent, settings: NotificationSettings) -> None:
    recipients = _emails_from_csv(settings.to_emails_csv)
    if not settings.smtp_host or not settings.from_email or not recipients:
        logger.debug('Skipping email send; SMTP configuration incomplete')
        return

    message = EmailMessage()
    message['Subject'] = event.subject
    message['From'] = settings.from_email
    message['To'] = ', '.join(recipients)
    message.set_content(event.body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def _send_worker() -> None:
    while not _stop_event.is_set():
        try:
            event = _email_queue.get(timeout=0.25)
        except Empty:
            continue

        db = SessionLocal()
        try:
            settings = get_or_create_notification_settings(db)
            _send_via_smtp(event, settings)
        except Exception:
            logger.exception('Failed to send email notification')
        finally:
            db.close()
            _email_queue.task_done()


def start_notification_worker() -> Thread:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return _worker_thread

    _stop_event.clear()
    _worker_thread = Thread(target=_send_worker, name='notification-sender', daemon=True)
    _worker_thread.start()
    return _worker_thread


def stop_notification_worker() -> None:
    global _worker_thread
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=5)
    _worker_thread = None


def enqueue_email(subject: str, body: str) -> None:
    _email_queue.put(EmailEvent(subject=subject, body=body))


def enqueue_test_email() -> None:
    enqueue_email('Optimizarr notification test', 'This is a test email from Optimizarr notifications.')


def enqueue_job_failed(job: Job) -> None:
    db = SessionLocal()
    try:
        settings = get_or_create_notification_settings(db)
        if not settings.notify_on_job_failed:
            return
        library_name = None
        if job.library_id:
            library = db.query(Library).filter(Library.id == job.library_id).first()
            library_name = library.name if library else None
    finally:
        db.close()

    file_name = Path(job.source_path).name
    body = _format_notification_body(
        library_name=library_name,
        file_name=file_name,
        reason=job.error_message or 'optimization_failed',
        suggested_action='Review the file and encoder settings, then retry the job.',
    )
    enqueue_email(subject='Optimizarr job failed', body=body)


def enqueue_job_interrupted(*, job_id: int, reason: str = 'Interrupted by application restart') -> None:
    db = SessionLocal()
    try:
        settings = get_or_create_notification_settings(db)
        if not settings.notify_on_job_interrupted:
            return

        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        library_name = None
        if job.library_id:
            library = db.query(Library).filter(Library.id == job.library_id).first()
            library_name = library.name if library else None

        file_name = Path(job.source_path).name
    finally:
        db.close()

    body = _format_notification_body(
        library_name=library_name,
        file_name=file_name,
        reason=reason,
        suggested_action='Run recovery and requeue interrupted jobs if needed.',
    )
    enqueue_email(subject='Optimizarr job interrupted', body=body)


def enqueue_low_disk_space_alert(*, min_free_gb: int, free_gb: float, library_name: str | None, file_name: str | None) -> None:
    db = SessionLocal()
    try:
        settings = get_or_create_notification_settings(db)
        if not settings.notify_on_low_disk_pause:
            return
    finally:
        db.close()

    body = _format_notification_body(
        library_name=library_name,
        file_name=file_name,
        reason=f'Queue paused for low disk space ({free_gb:.2f} GB free, minimum {min_free_gb} GB).',
        suggested_action='Free disk space in cache/workspace and resume the queue.',
    )
    enqueue_email(subject='Optimizarr queue paused: low cache space', body=body)


def enqueue_recovery_ran(*, trigger: str, recovered_jobs: int, requeued_jobs: int, cleaned_workspaces: int) -> None:
    db = SessionLocal()
    try:
        settings = get_or_create_notification_settings(db)
        if not settings.notify_on_recovery_ran:
            return
    finally:
        db.close()

    body = _format_notification_body(
        library_name='system',
        file_name='n/a',
        reason=(
            f'Recovery run ({trigger}). Recovered jobs: {recovered_jobs}, '
            f'Requeued jobs: {requeued_jobs}, Cleaned workspaces: {cleaned_workspaces}.'
        ),
        suggested_action='Review interrupted jobs and queue status in the dashboard.',
    )
    enqueue_email(subject='Optimizarr recovery completed', body=body)


def register_scan_batch(job_ids: list[int], library_name: str | None = None) -> None:
    if not job_ids:
        return
    with _batch_lock:
        _batches.append(BatchTracker(pending_ids=set(job_ids), library_name=library_name))


def handle_job_terminal_state(job_id: int, status: str) -> None:
    if status not in {'complete', 'failed', 'skipped', 'cancelled'}:
        return

    completed_batch: BatchTracker | None = None
    with _batch_lock:
        for batch in list(_batches):
            if job_id not in batch.pending_ids:
                continue

            batch.pending_ids.remove(job_id)
            batch.processed += 1
            if status == 'failed':
                batch.failed += 1

            if not batch.pending_ids:
                completed_batch = batch
                _batches.remove(batch)
            break

    if completed_batch is None:
        return

    db = SessionLocal()
    try:
        settings = get_or_create_notification_settings(db)
        if not settings.notify_on_batch_complete:
            return
    finally:
        db.close()

    body = _format_notification_body(
        library_name=completed_batch.library_name or 'multiple',
        file_name='n/a',
        reason=f'Batch complete. Processed: {completed_batch.processed}, Failed: {completed_batch.failed}.',
        suggested_action='Review failed items and retry any jobs that need another pass.',
    )
    enqueue_email(subject='Optimizarr batch complete', body=body)
