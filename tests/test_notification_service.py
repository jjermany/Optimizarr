from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job
from app.models.notification_settings import NotificationSettings
from app.services import notification_service


class DummySMTP:
    sent_messages = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls_started = False
        self.logged_in = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.tls_started = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        DummySMTP.sent_messages.append(message)


def test_send_via_smtp_builds_html_email(monkeypatch):
    DummySMTP.sent_messages.clear()

    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    settings = NotificationSettings(
        smtp_host='smtp.example.com',
        smtp_port=2525,
        smtp_user='mailer',
        smtp_password='secret',
        smtp_tls=True,
        from_email='optimizarr@example.com',
        to_emails_csv='ops@example.com',
    )

    event = notification_service.EmailEvent(
        subject='Optimizarr job failed',
        body='Library: Movies\nFile: film.mkv\nReason: codec mismatch\nSuggested action: retry\n',
    )

    notification_service._send_via_smtp(event, settings)

    assert len(DummySMTP.sent_messages) == 1
    sent = DummySMTP.sent_messages[0]
    rendered = sent.as_string()
    assert 'Content-Type: text/plain' in rendered
    assert 'Content-Type: text/html' in rendered
    assert 'optimizarr-logo' not in rendered

    html_parts = [part for part in sent.walk() if part.get_content_type() == 'text/html']
    assert len(html_parts) == 1
    assert 'Automated notification from Optimizarr.' in html_parts[0].get_content()


def test_format_notification_html_renders_text_branding():
    html = notification_service._format_notification_html(
        subject='Optimizarr notification test',
        body='Library: Movies\nReason: test',
    )

    assert 'cid:optimizarr-logo' not in html
    assert 'OPTIMIZARR' in html
    assert 'Optimizarr notification test' in html
    assert 'Library' in html
    assert 'Reason' in html


def test_format_notification_body_expands_known_failure_reason():
    body = notification_service._format_notification_body(
        library_name='Movies',
        file_name='film.mkv',
        reason='qsv_encode_failed',
        suggested_action='Retry with software encoder.',
    )

    assert 'qsv_encode_failed (Intel Quick Sync Video (QSV) failed to initialize or encode on this host.)' in body


def test_format_notification_body_keeps_unknown_failure_reason_as_is():
    body = notification_service._format_notification_body(
        library_name='Movies',
        file_name='film.mkv',
        reason='custom_error',
        suggested_action='Retry.',
    )

    assert 'Reason: custom_error' in body


def test_format_duration_formats_human_readable_runtime():
    assert notification_service.format_duration(5) == '5s'
    assert notification_service.format_duration(65) == '1m 5s'
    assert notification_service.format_duration(3665) == '1h 1m 5s'


def test_enqueue_job_failed_is_grouped_when_part_of_batch(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_email', lambda subject, body: queued.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_batch_complete = True
        db.commit()

    notification_service._batches.clear()
    notification_service.register_scan_batch([42], library_name='Movies')

    job = type('JobStub', (), {'id': 42, 'source_path': '/media/Movies/title.mkv', 'error_message': 'optimization_failed', 'library_id': None})

    notification_service.enqueue_job_failed(job)
    assert queued == []

    notification_service.handle_job_terminal_state(42, 'failed')
    assert len(queued) == 1
    assert queued[0][0] == 'Optimizarr batch complete'
    assert 'title.mkv' in queued[0][1]
    assert 'grouped alert' in queued[0][1]


def test_enqueue_job_complete_marks_encode_and_includes_runtime(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_email', lambda subject, body: queued.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_job_complete = True
        db.commit()

    job = type(
        'JobStub',
        (),
        {
            'source_path': '/media/Movies/title.mkv',
            'library_id': None,
            'encode_duration_seconds': 3723,
        },
    )

    notification_service.enqueue_job_complete(job)

    assert queued == [
        (
            'Optimizarr job complete',
            'Job Type: Encode\n'
            'Library: unknown\n'
            'File: title\n'
            'Encode Time: 1h 2m 3s\n'
            'Status: Completed successfully.\n',
        )
    ]


def test_enqueue_download_job_complete_marks_download_type(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_email', lambda subject, body: queued.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_job_complete = True
        db.commit()

    download_job = type(
        'DownloadJobStub',
        (),
        {
            'source_file_path': '/downloads/Movie.2024.mkv',
            'library_id': None,
        },
    )

    notification_service.enqueue_download_job_complete(download_job)

    assert queued == [
        (
            'Optimizarr job complete',
            'Job Type: Download\n'
            'Library: unknown\n'
            'File: Movie (2024)\n'
            'Status: Download imported successfully.\n',
        )
    ]


def test_handle_job_terminal_state_marks_waiting_encode_as_fallback_queued(monkeypatch):
    monkeypatch.setattr(notification_service.broker, 'publish', lambda *_args, **_kwargs: None)

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.commit()

        encode_job = Job(input_path='/media/Shadow of God (2025).mkv', status='complete')
        db.add(encode_job)
        db.commit()
        db.refresh(encode_job)
        encode_job_id = encode_job.id

        dj = DownloadJob(
            source_file_path='/media/Shadow of God (2025).mkv',
            status=DownloadJobStatus.waiting_encode.value,
            encode_job_id=encode_job.id,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)
        dj_id = dj.id

    notification_service.handle_job_terminal_state(encode_job_id, 'complete')

    with SessionLocal() as db:
        updated = db.query(DownloadJob).filter(DownloadJob.id == dj_id).first()
        assert updated is not None
        assert updated.status == DownloadJobStatus.fallback_queued.value
        assert updated.completed_at is not None


def test_handle_job_terminal_state_marks_waiting_encode_as_failed_when_encode_fails(monkeypatch):
    monkeypatch.setattr(notification_service.broker, 'publish', lambda *_args, **_kwargs: None)

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.commit()

        encode_job = Job(input_path='/media/Shadow of God (2025).mkv', status='failed')
        db.add(encode_job)
        db.commit()
        db.refresh(encode_job)
        encode_job_id = encode_job.id

        dj = DownloadJob(
            source_file_path='/media/Shadow of God (2025).mkv',
            status=DownloadJobStatus.waiting_encode.value,
            encode_job_id=encode_job.id,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)
        dj_id = dj.id

    notification_service.handle_job_terminal_state(encode_job_id, 'failed')

    with SessionLocal() as db:
        updated = db.query(DownloadJob).filter(DownloadJob.id == dj_id).first()
        assert updated is not None
        assert updated.status == DownloadJobStatus.failed.value
        assert updated.error_message == 'Fallback encode failed'
        assert updated.completed_at is not None
