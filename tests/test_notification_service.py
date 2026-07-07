import httpx
import pytest

from app.core import secrets_store
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
        email_enabled=True,
        smtp_host='smtp.example.com',
        smtp_port=2525,
        smtp_user='mailer',
        smtp_password='secret',
        smtp_tls=True,
        from_email='optimizarr@example.com',
        to_emails_csv='ops@example.com',
    )

    event = notification_service.NotificationEvent(
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


def test_send_via_smtp_skips_when_email_disabled(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    settings = NotificationSettings(
        email_enabled=False,
        smtp_host='smtp.example.com',
        from_email='optimizarr@example.com',
        to_emails_csv='ops@example.com',
    )
    event = notification_service.NotificationEvent(subject='subject', body='body')

    notification_service._send_via_smtp(event, settings)

    assert DummySMTP.sent_messages == []


def test_dispatch_notification_event_skips_send_if_toggle_disabled_after_enqueue(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        settings.notify_on_job_complete = True
        db.commit()
    finally:
        db.close()

    event = notification_service.NotificationEvent(
        subject='Optimizarr job complete',
        body='Status: Completed successfully.\n',
        kind='job_complete',
    )

    # Simulate the toggle being turned off after the job's completion email
    # was already enqueued but before the background worker sends it.
    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_job_complete = False
        db.commit()
    finally:
        db.close()

    notification_service._dispatch_notification_event(event)

    assert DummySMTP.sent_messages == []


def test_dispatch_notification_event_skips_send_for_unknown_kind(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        db.commit()
    finally:
        db.close()

    # A kind that isn't in _NOTIFY_FLAG_BY_KIND (e.g. a typo, or a new
    # enqueue_* call site that forgot to register its flag) must fail closed
    # instead of silently bypassing every notification preference toggle.
    event = notification_service.NotificationEvent(
        subject='Optimizarr unknown event',
        body='Status: n/a\n',
        kind='not_a_real_kind',
    )

    notification_service._dispatch_notification_event(event)

    assert DummySMTP.sent_messages == []


def test_dispatch_notification_event_sends_test_email_regardless_of_kind(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.email_enabled = True
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        db.commit()
    finally:
        db.close()

    # kind=None is the explicit bypass used by enqueue_test_notification(); it must
    # keep sending regardless of any notification toggle.
    event = notification_service.NotificationEvent(subject='Optimizarr notification test', body='test\n', kind=None)

    notification_service._dispatch_notification_event(event)

    assert len(DummySMTP.sent_messages) == 1


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
    monkeypatch.setattr(notification_service, 'enqueue_notification', lambda subject, body, kind=None: queued.append((subject, body)))

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
    assert 'Failed Files: title' in queued[0][1]
    assert 'Status: Completed with failures.' in queued[0][1]


def test_enqueue_job_failed_sends_individual_when_batch_digest_disabled(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_notification', lambda subject, body, kind=None: queued.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_batch_complete = False
        settings.notify_on_job_failed = True
        db.commit()

    notification_service._batches.clear()
    notification_service.register_scan_batch([42], library_name='Movies')

    job = type('JobStub', (), {'id': 42, 'source_path': '/media/Movies/title.mkv', 'error_message': 'optimization_failed', 'library_id': None})

    notification_service.enqueue_job_failed(job)
    notification_service.handle_job_terminal_state(42, 'failed')

    assert len(queued) == 1
    assert queued[0][0] == 'Optimizarr job failed'
    assert 'File: title' in queued[0][1]


def test_enqueue_job_complete_is_suppressed_when_batch_digest_enabled(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_notification', lambda subject, body, kind=None: queued.append((subject, body)))

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.notify_on_batch_complete = True
        settings.notify_on_job_complete = True
        db.commit()

    notification_service._batches.clear()
    notification_service.register_scan_batch([42], library_name='Movies')

    job = type(
        'JobStub',
        (),
        {
            'id': 42,
            'source_path': '/media/Movies/title.mkv',
            'library_id': None,
            'encode_duration_seconds': 60,
        },
    )

    notification_service.enqueue_job_complete(job)
    notification_service.handle_job_terminal_state(42, 'complete')

    assert len(queued) == 1
    assert queued[0][0] == 'Optimizarr batch complete'
    assert 'Completed: 1' in queued[0][1]


def test_enqueue_job_complete_marks_encode_and_includes_runtime(monkeypatch):
    queued = []
    monkeypatch.setattr(notification_service, 'enqueue_notification', lambda subject, body, kind=None: queued.append((subject, body)))

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
    monkeypatch.setattr(notification_service, 'enqueue_notification', lambda subject, body, kind=None: queued.append((subject, body)))

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


class FakePushoverResponse:
    def raise_for_status(self):
        return None


def _pushover_settings(**overrides):
    values = {
        'pushover_enabled': True,
        'pushover_api_token': secrets_store.encrypt_secret('app-token'),
        'pushover_user_key': secrets_store.encrypt_secret('user-key'),
    }
    values.update(overrides)
    return NotificationSettings(**values)


def test_send_via_pushover_posts_token_user_title_message(monkeypatch):
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append((url, data))
        return FakePushoverResponse()

    monkeypatch.setattr(notification_service.httpx, 'post', fake_post)

    event = notification_service.NotificationEvent(subject='Optimizarr job failed', body='Reason: codec mismatch\n')
    notification_service._send_via_pushover(event, _pushover_settings())

    assert len(calls) == 1
    url, data = calls[0]
    assert url == 'https://api.pushover.net/1/messages.json'
    assert data == {
        'token': 'app-token',
        'user': 'user-key',
        'title': 'Optimizarr job failed',
        'message': 'Reason: codec mismatch\n',
    }


def test_send_via_pushover_skips_when_disabled_or_unconfigured(monkeypatch):
    calls = []
    monkeypatch.setattr(notification_service.httpx, 'post', lambda *args, **kwargs: calls.append(args) or FakePushoverResponse())

    event = notification_service.NotificationEvent(subject='subject', body='body')

    notification_service._send_via_pushover(event, _pushover_settings(pushover_enabled=False))
    notification_service._send_via_pushover(event, _pushover_settings(pushover_api_token=''))
    notification_service._send_via_pushover(event, _pushover_settings(pushover_user_key=''))

    assert calls == []


def test_send_via_pushover_truncates_title_and_message(monkeypatch):
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(data)
        return FakePushoverResponse()

    monkeypatch.setattr(notification_service.httpx, 'post', fake_post)

    event = notification_service.NotificationEvent(subject='s' * 300, body='b' * 2000)
    notification_service._send_via_pushover(event, _pushover_settings())

    assert len(calls[0]['title']) == 250
    assert len(calls[0]['message']) == 1024


def test_send_via_pushover_raises_with_api_errors_on_rejection(monkeypatch):
    def fake_post(url, data=None, timeout=None):
        request = httpx.Request('POST', url)
        return httpx.Response(400, json={'status': 0, 'errors': ['application token is invalid']}, request=request)

    monkeypatch.setattr(notification_service.httpx, 'post', fake_post)

    event = notification_service.NotificationEvent(subject='subject', body='body')
    with pytest.raises(RuntimeError, match='application token is invalid'):
        notification_service._send_via_pushover(event, _pushover_settings())


def test_dispatch_notification_event_sends_both_channels(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    pushover_calls = []

    def fake_post(url, data=None, timeout=None):
        pushover_calls.append(data)
        return FakePushoverResponse()

    monkeypatch.setattr(notification_service.httpx, 'post', fake_post)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.email_enabled = True
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        settings.pushover_enabled = True
        settings.pushover_api_token = secrets_store.encrypt_secret('app-token')
        settings.pushover_user_key = secrets_store.encrypt_secret('user-key')
        db.commit()
    finally:
        db.close()

    event = notification_service.NotificationEvent(subject='Optimizarr notification test', body='test\n', kind=None)
    notification_service._dispatch_notification_event(event)

    assert len(DummySMTP.sent_messages) == 1
    assert len(pushover_calls) == 1


def test_dispatch_notification_event_respects_channel_filter(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    pushover_calls = []

    def fake_post(url, data=None, timeout=None):
        pushover_calls.append(data)
        return FakePushoverResponse()

    monkeypatch.setattr(notification_service.httpx, 'post', fake_post)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.email_enabled = True
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        settings.pushover_enabled = True
        settings.pushover_api_token = secrets_store.encrypt_secret('app-token')
        settings.pushover_user_key = secrets_store.encrypt_secret('user-key')
        db.commit()
    finally:
        db.close()

    # channel='pushover' must not touch SMTP even though email is enabled.
    notification_service._dispatch_notification_event(
        notification_service.NotificationEvent(subject='test', body='test\n', kind=None, channel='pushover')
    )
    assert DummySMTP.sent_messages == []
    assert len(pushover_calls) == 1

    # channel='email' must not touch Pushover.
    notification_service._dispatch_notification_event(
        notification_service.NotificationEvent(subject='test', body='test\n', kind=None, channel='email')
    )
    assert len(DummySMTP.sent_messages) == 1
    assert len(pushover_calls) == 1


def test_dispatch_notification_event_channel_failures_are_isolated(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setattr(notification_service.smtplib, 'SMTP', DummySMTP)

    pushover_calls = []

    def failing_post(url, data=None, timeout=None):
        pushover_calls.append(data)
        raise RuntimeError('pushover down')

    monkeypatch.setattr(notification_service.httpx, 'post', failing_post)

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.email_enabled = True
        settings.smtp_host = 'smtp.example.com'
        settings.from_email = 'optimizarr@example.com'
        settings.to_emails_csv = 'ops@example.com'
        settings.pushover_enabled = True
        settings.pushover_api_token = secrets_store.encrypt_secret('app-token')
        settings.pushover_user_key = secrets_store.encrypt_secret('user-key')
        db.commit()
    finally:
        db.close()

    event = notification_service.NotificationEvent(subject='Optimizarr notification test', body='test\n', kind=None)
    # Pushover raising must not prevent the email send or escape the dispatcher.
    notification_service._dispatch_notification_event(event)

    assert len(DummySMTP.sent_messages) == 1
    assert len(pushover_calls) == 1

    # And an SMTP failure must not prevent the Pushover send.
    def failing_smtp(*args, **kwargs):
        raise RuntimeError('smtp down')

    monkeypatch.setattr(notification_service.smtplib, 'SMTP', failing_smtp)
    monkeypatch.setattr(notification_service.httpx, 'post', lambda url, data=None, timeout=None: pushover_calls.append(data) or FakePushoverResponse())

    notification_service._dispatch_notification_event(event)
    assert len(pushover_calls) == 2


def test_settings_payload_masks_and_updates_pushover_secrets():
    db = SessionLocal()
    try:
        settings = notification_service.update_settings(db, {
            'pushover_enabled': True,
            'pushover_api_token': 'app-token',
            'pushover_user_key': 'user-key',
        })

        assert secrets_store.is_encrypted_secret(settings.pushover_api_token)
        assert secrets_store.is_encrypted_secret(settings.pushover_user_key)

        payload = notification_service.settings_to_payload(settings)
        assert payload['pushover_enabled'] is True
        assert payload['pushover_api_token'] == secrets_store.SECRET_MASK
        assert payload['pushover_user_key'] == secrets_store.SECRET_MASK

        # Round-tripping the masked payload must not clobber the stored secrets.
        before_token = settings.pushover_api_token
        settings = notification_service.update_settings(db, {
            'pushover_enabled': True,
            'pushover_api_token': secrets_store.SECRET_MASK,
            'pushover_user_key': secrets_store.SECRET_MASK,
        })
        assert settings.pushover_api_token == before_token
        assert secrets_store.decrypt_secret(settings.pushover_api_token) == 'app-token'
        assert secrets_store.decrypt_secret(settings.pushover_user_key) == 'user-key'
    finally:
        db.close()


def test_get_or_create_settings_encrypts_plaintext_pushover_secrets():
    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.pushover_api_token = 'plain-token'
        settings.pushover_user_key = 'plain-user'
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        settings = notification_service.get_or_create_notification_settings(db)
        assert secrets_store.is_encrypted_secret(settings.pushover_api_token)
        assert secrets_store.is_encrypted_secret(settings.pushover_user_key)
        assert secrets_store.decrypt_secret(settings.pushover_api_token) == 'plain-token'
        assert secrets_store.decrypt_secret(settings.pushover_user_key) == 'plain-user'
    finally:
        db.close()
