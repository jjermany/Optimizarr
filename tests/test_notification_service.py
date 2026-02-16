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


def test_send_via_smtp_builds_html_email_with_inline_logo(monkeypatch, tmp_path):
    DummySMTP.sent_messages.clear()

    logo_path = tmp_path / 'logo.png'
    logo_path.write_bytes(b'\x89PNG\r\n\x1a\nlogo')

    monkeypatch.setattr(notification_service, '_LOGO_CANDIDATE_PATHS', (logo_path,))
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
    assert 'optimizarr-logo' in rendered
    assert 'Content-ID: <optimizarr-logo>' in rendered

    html_parts = [part for part in sent.walk() if part.get_content_type() == 'text/html']
    assert len(html_parts) == 1
    assert 'Automated notification from Optimizarr.' in html_parts[0].get_content()


def test_format_notification_html_falls_back_to_heading_without_logo():
    html = notification_service._format_notification_html(
        subject='Optimizarr notification test',
        body='Library: Movies\nReason: test',
        include_logo=False,
    )

    assert 'cid:optimizarr-logo' not in html
    assert 'Optimizarr notification test' in html
    assert 'Library' in html
    assert 'Reason' in html
