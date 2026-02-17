from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
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
    failed_files: list[str] | None = None


_stop_event = Event()
_email_queue: Queue[EmailEvent] = Queue()
_worker_thread: Thread | None = None
_batch_lock = Lock()
_batches: list[BatchTracker] = []
_LOGO_CID = 'optimizarr-logo'
_LOGO_CANDIDATE_PATHS = (
    Path('/media/Logo/logo.png'),
    Path(__file__).resolve().parents[2] / 'media' / 'Logo' / 'logo.png',
)
_FAILURE_REASON_HINTS: dict[str, str] = {
    'qsv_encode_failed': 'Intel Quick Sync Video (QSV) failed to initialize or encode on this host.',
    'av1_encode_failed': 'The AV1 encoder failed during transcode.',
    'ffmpeg_unavailable': 'FFmpeg is missing or unavailable to the Optimizarr process.',
    'ffprobe_failed': 'FFprobe could not read media metadata for the source file.',
    'workspace_prepare_failed': 'Optimizarr could not prepare its temporary workspace.',
    'commit_failed': 'Optimizarr could not move the completed output file into the final location.',
    'optimization_failed': 'The transcode failed for an unspecified reason.',
}


def _emails_to_csv(emails: Iterable[str]) -> str:
    return ','.join(email.strip() for email in emails if email.strip())


def _emails_from_csv(csv_value: str) -> list[str]:
    return [email.strip() for email in csv_value.split(',') if email.strip()]


def _format_notification_body(*, library_name: str | None, file_name: str | None, reason: str, suggested_action: str) -> str:
    reason_detail = _FAILURE_REASON_HINTS.get(reason)
    reason_line = reason if reason_detail is None else f'{reason} ({reason_detail})'
    return (
        f'Library: {library_name or "unknown"}\n'
        f'File: {file_name or "n/a"}\n'
        f'Reason: {reason_line}\n'
        f'Suggested action: {suggested_action}\n'
    )


def _resolve_logo_path() -> Path | None:
    for path in _LOGO_CANDIDATE_PATHS:
        if path.exists():
            return path
    return None


def _notification_rows(body: str) -> str:
    rows: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ':' in line:
            label, value = line.split(':', 1)
            rows.append(
                '<tr>'
                f'<td style="padding:10px 12px;border-bottom:1px solid #27344d;color:#93c5fd;font-weight:600;">{escape(label.strip())}</td>'
                f'<td style="padding:10px 12px;border-bottom:1px solid #27344d;color:#e2e8f0;">{escape(value.strip())}</td>'
                '</tr>'
            )
        else:
            rows.append(
                '<tr><td colspan="2" style="padding:10px 12px;border-bottom:1px solid #27344d;color:#e2e8f0;">'
                f'{escape(line)}</td></tr>'
            )
    return ''.join(rows)


def _format_notification_html(*, subject: str, body: str, include_logo: bool) -> str:
    logo_html = (
        f'<img src="cid:{_LOGO_CID}" alt="Optimizarr" width="220" '
        'style="display:block;height:auto;max-width:100%;margin:0 auto 18px auto;" />'
        if include_logo
        else '<h1 style="margin:0 0 18px 0;text-align:center;font-size:30px;color:#67e8f9;">Optimizarr</h1>'
    )

    return (
        '<!doctype html>'
        '<html><body style="margin:0;padding:24px;background:#020617;font-family:Inter,Segoe UI,Roboto,Arial,sans-serif;">'
        '<table role="presentation" style="width:100%;max-width:680px;margin:0 auto;background:#0f172a;'
        'border:1px solid #1e293b;border-radius:16px;overflow:hidden;box-shadow:0 12px 30px rgba(2,6,23,0.5);">'
        '<tr><td style="padding:28px 26px 18px 26px;background:linear-gradient(135deg,#082f49,#164e63 55%,#0f172a);">'
        f'{logo_html}'
        f'<h2 style="margin:0;text-align:center;font-size:22px;color:#e0f2fe;">{escape(subject)}</h2>'
        '</td></tr>'
        '<tr><td style="padding:22px 26px;color:#cbd5e1;line-height:1.6;">'
        '<p style="margin:0 0 14px 0;color:#94a3b8;font-size:14px;">Automated notification from Optimizarr.</p>'
        '<table role="presentation" style="width:100%;border-collapse:collapse;background:#0b1220;border:1px solid #27344d;'
        'border-radius:10px;overflow:hidden;">'
        f'{_notification_rows(body)}'
        '</table>'
        '</td></tr>'
        '<tr><td style="padding:16px 26px 24px 26px;color:#64748b;font-size:12px;text-align:center;">'
        'Sent by Optimizarr • This mailbox may be unattended.'
        '</td></tr>'
        '</table></body></html>'
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

    logo_path = _resolve_logo_path()
    html_body = _format_notification_html(subject=event.subject, body=event.body, include_logo=bool(logo_path))
    message.add_alternative(html_body, subtype='html')

    if logo_path:
        logo_bytes = logo_path.read_bytes()
        html_part = message.get_payload()[-1]
        html_part.add_related(logo_bytes, maintype='image', subtype='png', cid=f'<{_LOGO_CID}>', filename=logo_path.name)

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
    file_name = Path(job.source_path).name
    with _batch_lock:
        for batch in _batches:
            if job.id not in batch.pending_ids:
                continue
            if batch.failed_files is None:
                batch.failed_files = []
            batch.failed_files.append(file_name)
            return

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
        file_name=', '.join(completed_batch.failed_files or []) or 'n/a',
        reason=(
            f'Batch complete. Processed: {completed_batch.processed}, Failed: {completed_batch.failed}.'
            if completed_batch.failed == 0
            else f'Batch complete. Processed: {completed_batch.processed}, Failed: {completed_batch.failed} (grouped alert).'
        ),
        suggested_action='Review failed items and retry any jobs that need another pass.',
    )
    enqueue_email(subject='Optimizarr batch complete', body=body)
