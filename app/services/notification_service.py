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
from app.models.notification_settings import NotificationSettings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailEvent:
    subject: str
    body: str


@dataclass(slots=True)
class BatchTracker:
    pending_ids: set[int]
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
            'job_complete': settings.notify_on_job_complete,
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
    if 'job_complete' in notify_on:
        settings.notify_on_job_complete = bool(notify_on['job_complete'])
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
    finally:
        db.close()

    file_name = Path(job.source_path).name
    body = (
        f'Job failed for file: {file_name}\n'
        f'Error: {job.error_message or "unknown_error"}\n'
        f'Encoder used: {job.encoder_used or "unknown"}\n'
        f'Fallback reason: {job.fallback_reason or "none"}\n'
    )
    enqueue_email(subject='Optimizarr job failed', body=body)



def enqueue_low_disk_space_alert(min_free_gb: int, free_gb: float) -> None:
    enqueue_email(
        subject='Optimizarr queue paused: low cache space',
        body=(
            'Queue was paused because workspace cache free space is below threshold.\n'
            f'Minimum free space: {min_free_gb} GB\n'
            f'Current free space: {free_gb:.2f} GB\n'
        ),
    )

def register_scan_batch(job_ids: list[int]) -> None:
    if not job_ids:
        return
    with _batch_lock:
        _batches.append(BatchTracker(pending_ids=set(job_ids)))


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

    enqueue_email(
        subject='Optimizarr batch complete',
        body=f'Batch complete. Processed: {completed_batch.processed}, Failed: {completed_batch.failed}',
    )
