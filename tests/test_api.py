import os
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import routes
from app.core.database import SessionLocal
from app.models.auth import AdminUser, AuthSession
from app.models.library import Library, LibraryProfile
from app.services import discovery_service, optimization_service
from app.main import app

BOOTSTRAP_TOKEN = 'test-bootstrap-token'


def _clear_auth_state() -> None:
    with SessionLocal() as db:
        db.query(AuthSession).delete()
        db.query(AdminUser).delete()
        db.commit()


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get('/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_api_prefixed_health_endpoint():
    with TestClient(app) as client:
        response = client.get('/api/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_queue_snapshot_has_realtime_revision_and_both_record_sets(monkeypatch):
    _clear_auth_state()
    monkeypatch.setattr(routes, '_sab_queue_positions_by_nzo', lambda db: {})

    with TestClient(app) as client:
        response = client.get('/queue/snapshot')

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload['revision'], int)
    assert isinstance(payload['jobs'], list)
    assert isinstance(payload['download_jobs'], list)
    assert payload['queue_status'] in {'running', 'paused'}


def test_version_endpoint():
    with TestClient(app) as client:
        response = client.get('/version')

    assert response.status_code == 200
    assert 'version' in response.json()




def test_branding_asset_endpoint_returns_logo(monkeypatch, tmp_path):
    logo_dir = tmp_path / 'Logo'
    logo_dir.mkdir(parents=True)
    (logo_dir / 'logo.png').write_bytes(b'logo-bytes')
    monkeypatch.setattr(routes, 'BRANDING_ROOTS', (logo_dir,))

    with TestClient(app) as client:
        response = client.get('/branding/logo')

    assert response.status_code == 200
    assert response.content == b'logo-bytes'


def test_branding_asset_endpoint_supports_svg_logo_variant(monkeypatch, tmp_path):
    logo_dir = tmp_path / 'Logo'
    logo_dir.mkdir(parents=True)
    (logo_dir / 'logo.svg').write_text('<svg>logo</svg>', encoding='utf-8')
    monkeypatch.setattr(routes, 'BRANDING_ROOTS', (logo_dir,))

    with TestClient(app) as client:
        response = client.get('/branding/logo')

    assert response.status_code == 200
    assert response.text == '<svg>logo</svg>'


def test_branding_asset_endpoint_prefers_dynamic_icon(monkeypatch, tmp_path):
    logo_dir = tmp_path / 'Logo'
    logo_dir.mkdir(parents=True)
    (logo_dir / 'icon.png').write_bytes(b'static-icon')
    (logo_dir / 'dynamic-icon.svg').write_text('<svg></svg>', encoding='utf-8')
    monkeypatch.setattr(routes, 'BRANDING_ROOTS', (logo_dir,))

    with TestClient(app) as client:
        response = client.get('/branding/icon')

    assert response.status_code == 200
    assert response.text == '<svg></svg>'


def test_branding_asset_endpoint_serves_pushover_icon(monkeypatch, tmp_path):
    logo_dir = tmp_path / 'Logo'
    logo_dir.mkdir(parents=True)
    (logo_dir / 'pushover-icon.jpg').write_bytes(b'jpeg-bytes')
    monkeypatch.setattr(routes, 'BRANDING_ROOTS', (logo_dir,))

    with TestClient(app) as client:
        response = client.get('/branding/pushover-icon')

    assert response.status_code == 200
    assert response.content == b'jpeg-bytes'


def test_branding_pushover_icon_asset_is_bundled():
    # The repo-bundled fallback root must actually contain the icon the
    # settings UI offers for download.
    with TestClient(app) as client:
        response = client.get('/branding/pushover-icon')

    assert response.status_code == 200
    assert response.content[:3] == b'\xff\xd8\xff'


def test_branding_asset_endpoint_returns_404_for_unknown_asset():
    with TestClient(app) as client:
        response = client.get('/branding/not-real')

    assert response.status_code == 404


def test_favicon_endpoint_returns_branding_icon(monkeypatch, tmp_path):
    logo_dir = tmp_path / 'Logo'
    logo_dir.mkdir(parents=True)
    (logo_dir / 'icon.png').write_bytes(b'icon-bytes')
    monkeypatch.setattr(routes, 'BRANDING_ROOTS', (logo_dir,))

    with TestClient(app) as client:
        response = client.get('/favicon.ico')

    assert response.status_code == 200
    assert response.content == b'icon-bytes'


def test_metrics_endpoint(monkeypatch):
    monkeypatch.setattr(
        routes,
        'get_system_metrics',
        lambda db: {
            'gpu_video_percent': 11.0,
            'gpu_render_percent': 22.0,
            'cpu_percent': 33.0,
            'ram_percent': 44.0,
            'active_jobs': 2,
        },
    )

    with TestClient(app) as client:
        response = client.get('/metrics')

    assert response.status_code == 200
    assert response.json() == {
        'gpu_video_percent': 11.0,
        'gpu_render_percent': 22.0,
        'cpu_percent': 33.0,
        'ram_percent': 44.0,
        'active_jobs': 2,
    }



def test_get_and_update_settings():
    workspace_root = str(Path(os.environ['OPTIMIZARR_WORKSPACE_ROOT_BASE']) / 'optimizarr-cache-settings' / 'workspaces')

    with TestClient(app) as client:
        get_response = client.get('/settings')
        assert get_response.status_code == 200
        payload = get_response.json()
        assert 'enable_optimizer' in payload
        assert 'history_retention_days' in payload
        assert 'auto_discovery_enabled' in payload
        assert payload['discovery_method'] in {'interval', 'watcher'}
        assert payload['discovery_interval_minutes'] >= 1
        assert payload['queue_sort'] in {'default', 'newest', 'oldest', 'year_newest', 'year_oldest'}
        assert payload['workspace_root']
        assert payload['scan_probe_workers'] >= 1
        assert isinstance(payload['requeue_interrupted_jobs'], bool)
        assert isinstance(payload['cleanup_workspaces_on_startup'], bool)
        assert isinstance(payload['duplicate_cleanup_enabled'], bool)
        assert payload['duplicate_cleanup_interval_hours'] >= 1
        assert payload['min_free_gb'] >= 1
        assert payload['qbt_strike_check_interval_seconds'] >= 1
        assert payload['qbt_metadata_max_strikes'] >= 0
        assert payload['qbt_stalled_max_strikes'] >= 0
        assert payload['qbt_slow_min_speed_bps'] >= 0
        assert payload['qbt_slow_max_strikes'] >= 0
        assert isinstance(payload['qbt_slow_ignore_private'], bool)

        update_response = client.post(
            '/settings',
            json={
                'enable_optimizer': False,
                'max_workers': 2,
                'auto_discovery_enabled': False,
                'discovery_method': 'watcher',
                'discovery_interval_minutes': 15,
                'queue_sort': 'newest',
                'workspace_root': workspace_root,
                'scan_probe_workers': 9999,
                'requeue_interrupted_jobs': False,
                'cleanup_workspaces_on_startup': False,
                'duplicate_cleanup_enabled': True,
                'duplicate_cleanup_interval_hours': 12,
                'min_free_gb': 32,
                'qbt_strike_check_interval_seconds': 60,
                'qbt_metadata_max_strikes': 3,
                'qbt_stalled_max_strikes': 4,
                'qbt_slow_min_speed_bps': 1024,
                'qbt_slow_max_strikes': 5,
                'qbt_slow_ignore_private': True,
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated['enable_optimizer'] is False
        assert updated['max_workers'] == 2
        assert updated['auto_discovery_enabled'] is False
        assert updated['discovery_method'] == 'watcher'
        assert updated['discovery_interval_minutes'] == 15
        assert updated['queue_sort'] == 'newest'
        assert updated['workspace_root'] == workspace_root
        assert updated['scan_probe_workers'] == max(1, os.cpu_count() or 1)
        assert updated['requeue_interrupted_jobs'] is False
        assert updated['cleanup_workspaces_on_startup'] is False
        assert updated['duplicate_cleanup_enabled'] is True
        assert updated['duplicate_cleanup_interval_hours'] == 12
        assert updated['min_free_gb'] == 32
        assert updated['qbt_strike_check_interval_seconds'] == 60
        assert updated['qbt_metadata_max_strikes'] == 3
        assert updated['qbt_stalled_max_strikes'] == 4
        assert updated['qbt_slow_min_speed_bps'] == 1024
        assert updated['qbt_slow_max_strikes'] == 5
        assert updated['qbt_slow_ignore_private'] is True

        final_response = client.get('/settings')
        assert final_response.status_code == 200
        final_payload = final_response.json()
        assert final_payload['enable_optimizer'] is False


def test_get_libraries_includes_scan_state(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'shows'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Shows', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        monkeypatch.setattr(discovery_service, 'is_library_scan_active', lambda candidate_id: candidate_id == library_id)

        list_response = client.get('/libraries')

    assert list_response.status_code == 200
    assert list_response.json() == [{
        'id': library_id,
        'name': 'Shows',
        'path': str(library_path),
        'enabled': True,
        'scanning': True,
    }]


def test_get_and_update_notification_settings_and_test_endpoint(monkeypatch):
    queued = []
    monkeypatch.setattr(routes.notification_service, 'enqueue_test_notification', lambda: queued.append('sent'))

    with TestClient(app) as client:
        get_response = client.get('/notifications/settings')
        assert get_response.status_code == 200
        payload = get_response.json()
        assert 'smtp_host' in payload
        assert 'notify_on' in payload

        update_response = client.put(
            '/notifications/settings',
            json={
                'email_enabled': True,
                'smtp_host': 'smtp.example.com',
                'smtp_port': 2525,
                'smtp_user': 'user',
                'smtp_password': 'pass',
                'smtp_tls': True,
                'from_email': 'optimizarr@example.com',
                'to_emails': ['ops@example.com', 'alerts@example.com'],
                'notify_on': {
                    'job_complete': True,
                    'job_failed': True,
                    'job_interrupted': True,
                    'low_disk_pause': True,
                    'recovery_ran': True,
                    'batch_complete': True,
                },
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated['email_enabled'] is True
        assert updated['smtp_host'] == 'smtp.example.com'
        assert updated['smtp_port'] == 2525
        assert updated['to_emails'] == ['ops@example.com', 'alerts@example.com']
        assert updated['notify_on']['job_complete'] is True
        assert updated['notify_on']['job_failed'] is True
        assert updated['notify_on']['job_interrupted'] is True
        assert updated['notify_on']['low_disk_pause'] is True
        assert updated['notify_on']['recovery_ran'] is True
        assert updated['notify_on']['batch_complete'] is True

        toggle_response = client.put(
            '/notifications/settings',
            json={
                'smtp_tls': False,
                'notify_on': {
                    'job_complete': False,
                    'job_failed': False,
                    'job_interrupted': False,
                    'low_disk_pause': False,
                    'recovery_ran': False,
                    'batch_complete': False,
                },
            },
        )
        assert toggle_response.status_code == 200
        toggled = toggle_response.json()
        assert toggled['smtp_tls'] is False
        assert toggled['notify_on']['job_complete'] is False
        assert toggled['notify_on']['job_failed'] is False
        assert toggled['notify_on']['job_interrupted'] is False
        assert toggled['notify_on']['low_disk_pause'] is False
        assert toggled['notify_on']['recovery_ran'] is False
        assert toggled['notify_on']['batch_complete'] is False

        test_response = client.post('/notifications/test')
        assert test_response.status_code == 202
        assert test_response.json() == {'status': 'queued'}

        # With every agent disabled the test endpoint refuses instead of
        # silently queueing a notification that can never be delivered.
        disable_response = client.put('/notifications/settings', json={'email_enabled': False, 'pushover_enabled': False})
        assert disable_response.status_code == 200
        rejected = client.post('/notifications/test')
        assert rejected.status_code == 400

    assert queued == ['sent']


def test_per_agent_test_notification_endpoint(monkeypatch):
    queued = []
    monkeypatch.setattr(
        routes.notification_service,
        'enqueue_test_notification',
        lambda channel=None: queued.append(channel),
    )

    # Reset any agent config left behind by other tests sharing the DB. The
    # API refuses an empty smtp_host by design, so clear directly.
    from app.core.database import SessionLocal
    from app.services import notification_service

    with SessionLocal() as db:
        settings = notification_service.get_or_create_notification_settings(db)
        settings.email_enabled = False
        settings.pushover_enabled = False
        settings.smtp_host = ''
        settings.from_email = ''
        settings.to_emails_csv = ''
        settings.pushover_api_token = ''
        settings.pushover_user_key = ''
        db.commit()

    with TestClient(app) as client:

        # Disabled agents refuse with an actionable message.
        assert client.post('/notifications/test/email').status_code == 400
        assert client.post('/notifications/test/pushover').status_code == 400
        # Unknown agents are a 404, not a silent no-op.
        assert client.post('/notifications/test/discord').status_code == 404

        # Enabled but unconfigured agents still refuse.
        client.put('/notifications/settings', json={'email_enabled': True, 'pushover_enabled': True})
        unconfigured_email = client.post('/notifications/test/email')
        assert unconfigured_email.status_code == 400
        assert 'not fully configured' in unconfigured_email.json()['detail']
        assert client.post('/notifications/test/pushover').status_code == 400

        # Fully configured agents queue a channel-targeted test.
        client.put(
            '/notifications/settings',
            json={
                'smtp_host': 'smtp.example.com',
                'from_email': 'optimizarr@example.com',
                'to_emails': ['ops@example.com'],
                'pushover_api_token': 'app-token',
                'pushover_user_key': 'user-key',
            },
        )
        email_response = client.post('/notifications/test/email')
        assert email_response.status_code == 202
        assert email_response.json() == {'status': 'queued', 'agent': 'email'}
        pushover_response = client.post('/notifications/test/pushover')
        assert pushover_response.status_code == 202
        assert pushover_response.json() == {'status': 'queued', 'agent': 'pushover'}

    assert queued == ['email', 'pushover']


def test_auth_bootstrap_and_login_flow():
    _clear_auth_state()

    with TestClient(app) as client:
        status_before = client.get('/auth/status')
        assert status_before.status_code == 200
        assert status_before.json()['setup_required'] is True

        bootstrap = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': False,
            },
        )
        assert bootstrap.status_code == 201
        assert bootstrap.json()['username'] == 'admin'
        assert bootstrap.cookies.get('optimizarr_session')

        settings = client.get('/settings')
        assert settings.status_code == 200

        client.post('/auth/logout', headers={'X-CSRF-Token': client.cookies.get('optimizarr_csrf', '')})
        after_logout = client.get('/settings')
        assert after_logout.status_code == 401

        login = client.post('/auth/login', json={'username': 'admin', 'password': 'VeryStrongPassword123'})
        assert login.status_code == 200
        assert login.cookies.get('optimizarr_session')

        settings_after_login = client.get('/settings')
        assert settings_after_login.status_code == 200

    _clear_auth_state()


def test_auth_login_requires_totp_when_enabled():
    _clear_auth_state()

    with TestClient(app) as client:
        secret_response = client.post('/auth/totp/secret', json={'username': 'admin'})
        assert secret_response.status_code == 200
        secret_payload = secret_response.json()
        secret = secret_payload['secret']
        assert secret_payload['otpauth_url'].startswith('otpauth://totp/')
        assert f'secret={secret}' in secret_payload['otpauth_url']
        assert 'admin' in secret_payload['otpauth_url']

        bootstrap = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': True,
                'totp_secret': secret,
                'totp_code': routes.auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert bootstrap.status_code == 201
        client.post('/auth/logout', headers={'X-CSRF-Token': client.cookies.get('optimizarr_csrf', '')})

        missing_otp = client.post('/auth/login', json={'username': 'admin', 'password': 'VeryStrongPassword123'})
        assert missing_otp.status_code == 401
        assert 'Two-factor code required' in missing_otp.text

        wrong_otp = client.post('/auth/login', json={'username': 'admin', 'password': 'VeryStrongPassword123', 'otp_code': '000000'})
        assert wrong_otp.status_code == 401

        non_digit_otp = client.post(
            '/auth/login',
            json={'username': 'admin', 'password': 'VeryStrongPassword123', 'otp_code': 'abc123'},
        )
        assert non_digit_otp.status_code == 401

        ok = client.post(
            '/auth/login',
            json={
                'username': 'admin',
                'password': 'VeryStrongPassword123',
                'otp_code': routes.auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert ok.status_code == 200
        assert ok.cookies.get('optimizarr_session')

    _clear_auth_state()


def test_auth_account_update_and_enable_two_factor():
    _clear_auth_state()

    with TestClient(app) as client:
        bootstrap = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': False,
            },
        )
        assert bootstrap.status_code == 201

        csrf = client.cookies.get('optimizarr_csrf', '')
        account_before = client.get('/auth/account')
        assert account_before.status_code == 200
        assert account_before.json()['username'] == 'admin'
        assert account_before.json()['two_factor_enabled'] is False

        update = client.post(
            '/auth/account',
            headers={'X-CSRF-Token': csrf},
            json={
                'current_password': 'VeryStrongPassword123',
                'username': 'media-admin',
                'new_password': 'AnotherStrongPassword123',
            },
        )
        assert update.status_code == 200
        assert update.json()['username'] == 'media-admin'

        secret = client.post('/auth/totp/secret', json={'username': 'media-admin'})
        assert secret.status_code == 200
        secret_value = secret.json()['secret']

        enable_2fa = client.post(
            '/auth/account/2fa/enable',
            headers={'X-CSRF-Token': csrf},
            json={
                'current_password': 'AnotherStrongPassword123',
                'totp_secret': secret_value,
                'totp_code': routes.auth_service.current_totp_code(secret_value, at_time=int(time.time())),
            },
        )
        assert enable_2fa.status_code == 200
        assert enable_2fa.json()['two_factor_enabled'] is True

        disable_2fa = client.post(
            '/auth/account/2fa/disable',
            headers={'X-CSRF-Token': csrf},
            json={
                'current_password': 'AnotherStrongPassword123',
                'totp_code': routes.auth_service.current_totp_code(secret_value, at_time=int(time.time())),
            },
        )
        assert disable_2fa.status_code == 200
        assert disable_2fa.json()['two_factor_enabled'] is False

    _clear_auth_state()

    with TestClient(app) as client:
        secret_response = client.post('/auth/totp/secret', json={'username': 'admin'})
        assert secret_response.status_code == 200
        secret = secret_response.json()['secret']

        bootstrap = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': True,
                'totp_secret': secret,
                'totp_code': routes.auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert bootstrap.status_code == 201
        client.post('/auth/logout', headers={'X-CSRF-Token': client.cookies.get('optimizarr_csrf', '')})

        missing_otp = client.post('/auth/login', json={'username': 'admin', 'password': 'VeryStrongPassword123'})
        assert missing_otp.status_code == 401
        assert 'Two-factor code required' in missing_otp.text

        wrong_otp = client.post('/auth/login', json={'username': 'admin', 'password': 'VeryStrongPassword123', 'otp_code': '000000'})
        assert wrong_otp.status_code == 401

        ok = client.post(
            '/auth/login',
            json={
                'username': 'admin',
                'password': 'VeryStrongPassword123',
                'otp_code': routes.auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert ok.status_code == 200
        assert ok.cookies.get('optimizarr_session')

    _clear_auth_state()


def test_auth_bootstrap_requires_valid_setup_token():
    _clear_auth_state()

    with TestClient(app) as client:
        response = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': 'wrong-token',
                'password': 'VeryStrongPassword123',
                'enable_two_factor': False,
            },
        )

    assert response.status_code == 403
    assert 'setup token' in response.text.lower()


def test_auth_bootstrap_rejects_cross_origin_requests():
    _clear_auth_state()

    with TestClient(app) as client:
        response = client.post(
            '/auth/bootstrap',
            headers={'Origin': 'https://evil.example'},
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': False,
            },
        )

    assert response.status_code == 403
    assert 'cross-origin' in response.text.lower()


def test_create_update_delete_library_and_profile_endpoints(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'movies'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_response = client.post(
            '/libraries',
            json={'name': 'Movies', 'path': str(library_path), 'enabled': True},
        )
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        list_response = client.get('/libraries')
        assert list_response.status_code == 200
        assert any(item['id'] == library_id for item in list_response.json())

        profile_response = client.get(f'/libraries/{library_id}/profile')
        assert profile_response.status_code == 200
        assert profile_response.json()['output_suffix'] == '-1080p'
        assert profile_response.json()['hdr_only'] is True
        assert profile_response.json()['minimum_source_resolution'] == 2160
        assert profile_response.json()['schedule_policy'] == 'finish_current'
        assert profile_response.json()['output_conflict_policy'] == 'skip'

        low_crf_response = client.put(f'/libraries/{library_id}/profile', json={'bitrate_mode': 'vbr_crf', 'crf': 17})
        assert low_crf_response.status_code == 422

        high_crf_response = client.put(f'/libraries/{library_id}/profile', json={'bitrate_mode': 'vbr_crf', 'crf': 31})
        assert high_crf_response.status_code == 422

        update_profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={
                'hdr_only': True,
                'minimum_source_resolution': 3000,
                'codec': 'av1',
                'av1_fallback_codec': 'h264',
                'download_codec': 'hevc',
                'download_fallback_codec': 'h264',
                'max_workers': 2,
                'schedule_start_hour': 3,
                'schedule_end_hour': 11,
                'schedule_policy': 'pause_current',
                'output_conflict_policy': 'rename',
            },
        )
        assert update_profile_response.status_code == 200
        updated_profile = update_profile_response.json()
        assert updated_profile['hdr_only'] is True
        assert updated_profile['minimum_source_resolution'] == 3000
        assert updated_profile['codec'] == 'av1'
        assert updated_profile['av1_fallback_codec'] == 'h264'
        assert updated_profile['download_codec'] == 'hevc'
        assert updated_profile['download_fallback_codec'] == 'h264'
        assert updated_profile['schedule_start_hour'] == 3
        assert updated_profile['schedule_end_hour'] == 11
        assert updated_profile['schedule_policy'] == 'pause_current'
        assert updated_profile['output_conflict_policy'] == 'rename'

        invalid_path_response = client.post('/libraries', json={'name': 'Bad', 'path': '/tmp/not-allowed'})
        assert invalid_path_response.status_code == 422

        update_library_response = client.put(f'/libraries/{library_id}', json={'enabled': False})
        assert update_library_response.status_code == 200
        assert update_library_response.json()['enabled'] is False

        delete_response = client.delete(f'/libraries/{library_id}')
        assert delete_response.status_code == 204


def test_create_library_rejects_paths_outside_media_root(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        response = client.post(
            '/libraries',
            json={'name': 'Outside', 'path': str(outside), 'enabled': True},
        )

    assert response.status_code == 422
    assert 'within' in response.text.lower()


def test_fs_dirs_stays_within_media_root(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    (media_root / 'movies').mkdir(parents=True)
    (media_root / '.hidden').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        response = client.get('/fs/dirs', params={'path': str(outside)})

    assert response.status_code == 200
    assert response.json() == {
        'path': str(media_root.resolve()),
        'parent': None,
        'dirs': ['movies'],
    }


def test_update_settings_rejects_workspace_root_outside_allowed_base():
    outside_root = str(Path(os.environ['OPTIMIZARR_WORKSPACE_ROOT_BASE']).resolve().parent / 'optimizarr-workspaces-outside-base')

    with TestClient(app) as client:
        response = client.post('/settings', json={'workspace_root': outside_root})

    assert response.status_code == 422
    assert 'within' in response.text.lower()


def test_integration_settings_reject_hosts_with_paths():
    with TestClient(app) as client:
        plex_response = client.put('/plex/settings', json={'host': 'http://plex.local/admin', 'port': 32400})
        prowlarr_response = client.put('/prowlarr/settings', json={'host': 'http://prowlarr.local/api/v1'})
        qbt_response = client.put('/download-client/qbittorrent', json={'host': 'http://qbt.local/ui', 'port': 8080})
        sab_response = client.put('/download-client/sabnzbd', json={'host': 'http://sab.local/api', 'port': 8081})
        smtp_response = client.put('/notifications/settings', json={'smtp_host': 'smtp://mail.local'})

    assert plex_response.status_code == 422
    assert prowlarr_response.status_code == 422
    assert qbt_response.status_code == 422
    assert sab_response.status_code == 422
    assert smtp_response.status_code == 422


def test_sab_retry_limit_defaults_and_clamps_on_save():
    from app.core.database import SessionLocal
    from app.models.sabnzbd_settings import SabnzbdSettings

    with SessionLocal() as db:
        db.query(SabnzbdSettings).delete()
        db.commit()

    with TestClient(app) as client:
        default_response = client.get('/download-client/sabnzbd')
        high_response = client.put('/download-client/sabnzbd', json={'max_download_retries': 25})
        low_response = client.put('/download-client/sabnzbd', json={'max_download_retries': -4})
        client.put('/download-client/sabnzbd', json={'max_download_retries': 10})

    assert default_response.status_code == 200
    assert default_response.json()['max_download_retries'] == 10
    assert high_response.status_code == 200
    assert high_response.json()['max_download_retries'] == 20
    assert low_response.status_code == 200
    assert low_response.json()['max_download_retries'] == 0


def test_qbt_retry_limit_defaults_and_clamps_on_save():
    from app.core.database import SessionLocal
    from app.models.qbittorrent_settings import QBittorrentSettings

    with SessionLocal() as db:
        db.query(QBittorrentSettings).delete()
        db.commit()

    with TestClient(app) as client:
        default_response = client.get('/download-client/qbittorrent')
        high_response = client.put('/download-client/qbittorrent', json={'max_download_retries': 99})
        low_response = client.put('/download-client/qbittorrent', json={'max_download_retries': -2})
        client.put('/download-client/qbittorrent', json={'max_download_retries': 1})

    assert default_response.status_code == 200
    assert default_response.json()['max_download_retries'] == 1
    assert high_response.status_code == 200
    assert high_response.json()['max_download_retries'] == 20
    assert low_response.status_code == 200
    assert low_response.json()['max_download_retries'] == 0


def test_create_and_fetch_job():
    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/demo.mkv'})
        assert create_response.status_code == 201
        job = create_response.json()
        assert job['status'] in {'queued', 'starting', 'running', 'complete', 'failed', 'skipped', 'cancelled'}

        time.sleep(0.3)

        get_response = client.get(f"/jobs/{job['id']}")
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched['source_path'] == '/media/demo.mkv'
        assert fetched['status'] in {'queued', 'starting', 'running', 'complete', 'failed', 'skipped', 'cancelled'}


def test_delete_job_endpoint(monkeypatch):
    system_events = []
    monkeypatch.setattr('app.api.routes.broker.publish_system_event', lambda event, **data: system_events.append((event, data)))

    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/delete-me.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        delete_response = client.delete(f'/jobs/{job_id}')
        assert delete_response.status_code == 204

        get_response = client.get(f'/jobs/{job_id}')
        assert get_response.status_code == 404
        assert ('job_removed', {'job_id': job_id}) in system_events


def test_scan_cancel_and_retry_endpoints(monkeypatch, tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'lib'
    (library_path / 'nested').mkdir(parents=True)

    source_a = library_path / 'a.mkv'
    source_a.write_text('a')

    source_d = library_path / 'nested' / 'd.mp4'
    source_d.write_text('d')

    (library_path / 'b-1080p.mkv').write_text('already_optimized_name')

    source_c = library_path / 'c.mkv'
    source_c.write_text('c')
    (library_path / 'c-1080p.mkv').write_text('output_exists')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Shows', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201

        scan_response = client.post('/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']

        created_paths = {job['source_path'] for job in created_jobs}
        assert {str(source_a), str(source_d)}.issubset(created_paths)

        scoped_jobs = [job for job in created_jobs if job['source_path'].startswith(str(library_path))]
        target_job_id = scoped_jobs[0]['id']

        cancel_response = client.post(f'/jobs/{target_job_id}/cancel')
        assert cancel_response.status_code == 200
        cancelled = cancel_response.json()
        assert cancelled['status'] == 'queued'

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == target_job_id).first()
            assert job is not None
            assert job.completed_at is None
            assert job.cancel_requested is False

        retry_response = client.post(f'/jobs/{target_job_id}/retry')
        assert retry_response.status_code == 200
        retried = retry_response.json()
        assert retried['status'] == 'queued'


def test_cancel_download_job_resets_job_to_pending():
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.library import Library, LibraryProfile

    with TestClient(app) as client:
        with SessionLocal() as db:
            db.query(DownloadJob).delete()
            db.query(LibraryProfile).delete()
            db.query(Library).delete()
            db.commit()

            library = Library(name='Download Library', path='/media/downloads', enabled=True)
            db.add(library)
            db.commit()
            db.refresh(library)

            profile = LibraryProfile(library_id=library.id)
            db.add(profile)
            db.commit()

            dj = DownloadJob(
                library_id=library.id,
                source_file_path='/media/download-cancel.mkv',
                status=DownloadJobStatus.downloading.value,
                search_query='Movie 2024 1080p',
                release_name='Movie.2024.1080p.WEB-DL-GROUP',
                indexer_id=12,
                indexer_name='SomeIndexer',
                download_hash='abc123',
                client_type='manual',
                progress_percent=73,
                download_speed_bps=5_000_000,
                downloaded_file_path='/downloads/movie.mkv',
                imported_file_path='/imports/movie.mkv',
                error_message='temporary',
                encode_job_id=42,
            )
            db.add(dj)
            db.commit()
            db.refresh(dj)
            job_id = dj.id

        response = client.post(f'/download-jobs/{job_id}/cancel')

    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == DownloadJobStatus.pending.value
    assert payload['progress_percent'] == 0
    assert payload['error_message'] is None
    assert payload['release_name'] is None
    assert payload['indexer_id'] is None
    assert payload['indexer_name'] is None
    assert payload['download_hash'] is None
    assert payload['client_type'] is None
    assert payload['download_speed_bps'] is None
    assert payload['encode_job_id'] is None
    assert payload['completed_at'] is None

    with SessionLocal() as db:
        db_job = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
        assert db_job is not None
        assert db_job.status == DownloadJobStatus.pending.value
        assert db_job.completed_at is None
        assert db_job.error_message is None


def test_list_download_jobs_reports_partial_sab_backlog_item_as_queued(monkeypatch):
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.library import Library, LibraryProfile
    from app.models.sabnzbd_settings import SabnzbdSettings

    monkeypatch.setattr(
        routes.download_client_service,
        'get_sab_queue_items',
        lambda _sab: [
            {'nzo_id': 'SAB_ACTIVE', 'name': 'Active.Release', 'percentage': 50.0, 'status': 'Downloading', 'index': 0},
            {'nzo_id': 'SAB_QUEUED_PARTIAL', 'name': 'Queued.Partial.Release', 'percentage': 8.0, 'status': 'Queued', 'index': 3},
        ],
    )

    with TestClient(app) as client:
        with SessionLocal() as db:
            db.query(DownloadJob).delete()
            db.query(LibraryProfile).delete()
            db.query(Library).delete()
            db.query(SabnzbdSettings).delete()
            db.commit()

            sab = SabnzbdSettings(id=1, enabled=True, host='http://sab', port=8080, api_key='key')
            library = Library(name='Download Library', path='/media/downloads', enabled=True)
            db.add_all([sab, library])
            db.commit()
            db.refresh(library)

            profile = LibraryProfile(library_id=library.id)
            db.add(profile)
            db.commit()

            dj = DownloadJob(
                library_id=library.id,
                source_file_path='/media/queued-partial.mkv',
                status=DownloadJobStatus.downloading.value,
                download_hash='SAB_QUEUED_PARTIAL',
                client_type='sabnzbd',
                progress_percent=8,
                eta_seconds=None,
                download_speed_bps=None,
            )
            db.add(dj)
            db.commit()

        response = client.get('/download-jobs')

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]['status'] == DownloadJobStatus.queued.value
    assert payload[0]['progress_percent'] == 8
    assert payload[0]['client_queue_position'] == 3
    assert payload[0]['download_speed_bps'] == 0


def test_cancel_download_job_removes_active_qbit_torrent_when_enabled(monkeypatch):
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.library import Library, LibraryProfile
    from app.models.qbittorrent_settings import QBittorrentSettings

    removed_calls = []
    monkeypatch.setattr(
        routes.download_client_service,
        'remove_qbt_torrent',
        lambda _s, torrent_hash, **kwargs: removed_calls.append({'hash': torrent_hash, **kwargs}) or True,
    )

    with TestClient(app) as client:
        with SessionLocal() as db:
            db.query(DownloadJob).delete()
            db.query(LibraryProfile).delete()
            db.query(Library).delete()
            db.query(QBittorrentSettings).delete()
            db.commit()

            qbt = QBittorrentSettings(id=1, enabled=True, host='http://qbit', port=8080, username='u', password='p')
            db.add(qbt)

            library = Library(name='Download Library', path='/media/downloads', enabled=True)
            db.add(library)
            db.commit()
            db.refresh(library)

            profile = LibraryProfile(library_id=library.id)
            db.add(profile)
            db.commit()

            dj = DownloadJob(
                library_id=library.id,
                source_file_path='/media/download-remove.mkv',
                status=DownloadJobStatus.downloading.value,
                download_hash='abc123',
                client_type='qbittorrent',
                progress_percent=10,
            )
            db.add(dj)
            db.commit()
            db.refresh(dj)
            job_id = dj.id

        response = client.post(f'/download-jobs/{job_id}/cancel')

    assert response.status_code == 200
    assert removed_calls == [{'hash': 'abc123', 'delete_files': True}]


def test_scan_uses_enabled_libraries(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    enabled_library_path = media_root / 'shows'
    enabled_library_path.mkdir()
    disabled_library_path = media_root / 'movies'
    disabled_library_path.mkdir()

    source_enabled = enabled_library_path / 'episode.mkv'
    source_enabled.write_text('video')
    source_disabled = disabled_library_path / 'movie.mkv'
    source_disabled.write_text('video')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        enabled_library = client.post('/libraries', json={'name': 'Shows', 'path': str(enabled_library_path), 'enabled': True})
        assert enabled_library.status_code == 201
        enabled_library_id = enabled_library.json()['id']

        disabled_library = client.post('/libraries', json={'name': 'Movies', 'path': str(disabled_library_path), 'enabled': False})
        assert disabled_library.status_code == 201

        profile_update = client.put(f'/libraries/{enabled_library_id}/profile', json={'output_suffix': '-opt'})
        assert profile_update.status_code == 200

        from app.core.database import SessionLocal
        from app.models.event_log import EventLog

        with SessionLocal() as session:
            session.query(EventLog).delete()
            session.commit()

        scan_response = client.post('/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_enabled)

        from app.models.job import Job

        with SessionLocal() as session:
            job = session.query(Job).filter(Job.id == created_jobs[0]['id']).first()
            assert job is not None
            assert job.library_id == enabled_library_id
            assert job.profile_snapshot_json is not None

        logs_response = client.get('/logs')
        assert logs_response.status_code == 200
        logs = logs_response.json()
        assert logs[0]['event_type'] == 'library_scan_summary'
        assert 'Shows' in logs[0]['details']['library_names']
        assert 'Movies' not in logs[0]['details']['library_names']
        assert 'library_id' not in logs[0]['details']
        assert logs[1]['event_type'] == 'library_scan_started'
        assert 'Shows' in logs[1]['details']['library_names']
        assert 'Movies' not in logs[1]['details']['library_names']
        assert 'library_id' not in logs[1]['details']



def test_scan_library_endpoint_honors_hdr_height_and_idempotency(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library'
    library_path.mkdir()

    hdr_file = library_path / 'hdr.mkv'
    hdr_file.write_text('hdr')
    low_res_file = library_path / 'low.mp4'
    low_res_file.write_text('low')
    sdr_file = library_path / 'sdr.mkv'
    sdr_file.write_text('sdr')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda path: 1080 if path.endswith('low.mp4') else 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda path: path.endswith('hdr.mkv'))

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'HDR', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        profile_update = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'minimum_source_resolution': 2000, 'target_resolution': 1080},
        )
        assert profile_update.status_code == 200

        first_scan_response = client.post(f'/libraries/{library_id}/scan')
        assert first_scan_response.status_code == 200
        first_created_jobs = first_scan_response.json()['created_jobs']
        assert {job['source_path'] for job in first_created_jobs} == {str(hdr_file)}

        second_scan_response = client.post(f'/libraries/{library_id}/scan')
        assert second_scan_response.status_code == 200
        assert second_scan_response.json()['created_jobs'] == []

        from app.core.database import SessionLocal
        from app.models.job import Job

        with SessionLocal() as session:
            job = session.query(Job).filter(Job.id == first_created_jobs[0]['id']).first()
            assert job is not None
            first_snapshot = job.profile_snapshot_json

        post_edit_scan = client.put(f'/libraries/{library_id}/profile', json={'codec': 'av1'})
        assert post_edit_scan.status_code == 200

        with SessionLocal() as session:
            job = session.query(Job).filter(Job.id == first_created_jobs[0]['id']).first()
            assert job is not None
            assert job.profile_snapshot_json != first_snapshot
            assert '"codec": "av1"' in job.profile_snapshot_json


def test_scan_library_endpoint_allows_disabled_library_when_requested_manually(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library'
    library_path.mkdir()
    (library_path / 'movie.mkv').write_text('video')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Disabled', 'path': str(library_path), 'enabled': False})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(library_path / 'movie.mkv')


def test_scan_library_endpoint_resumes_queue_after_manual_scan(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library'
    library_path.mkdir()
    (library_path / 'episode.mkv').write_text('video')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    queue_events = []
    monkeypatch.setattr(routes.worker_queue, 'pause_queue', lambda reason='manual': queue_events.append(('pause', reason)))
    monkeypatch.setattr(routes.worker_queue, 'resume_queue', lambda reason='manual': queue_events.append(('resume', reason)))

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Resume Scan', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200

    assert queue_events == [('pause', 'manual_scan'), ('resume', 'manual_scan')]


def test_profile_update_rejects_min_resolution_equal_or_lower_than_target(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Validation', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        invalid_update = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'target_resolution': 1080, 'minimum_source_resolution': 1080},
        )
        assert invalid_update.status_code == 422


def test_profile_update_allows_equal_min_resolution_when_hdr_only_enabled(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library-hdr'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Validation HDR', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        valid_update = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'target_resolution': 1080, 'minimum_source_resolution': 1080},
        )
        assert valid_update.status_code == 200
        payload = valid_update.json()
        assert payload['hdr_only'] is True
        assert payload['target_resolution'] == 1080
        assert payload['minimum_source_resolution'] == 1080


def test_profile_update_allows_equal_min_resolution_when_tonemap_enabled(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library-tonemap'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Validation ToneMap', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        valid_update = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'tone_map_hdr': True, 'target_resolution': 1080, 'minimum_source_resolution': 1080},
        )
        assert valid_update.status_code == 200
        payload = valid_update.json()
        assert payload['hdr_only'] is False
        assert payload['tone_map_hdr'] is True
        assert payload['target_resolution'] == 1080
        assert payload['minimum_source_resolution'] == 1080


def test_profile_update_allows_combined_hdr_only_and_tonemap_flags(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library-conflict'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Validation Conflict', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        update = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'tone_map_hdr': True},
        )
        assert update.status_code == 200
        payload = update.json()
        assert payload['hdr_only'] is True
        assert payload['tone_map_hdr'] is True


def test_pause_resume_abort_and_queue_controls(monkeypatch, tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    workspace_root = tmp_path / 'workspaces'

    with TestClient(app) as client:
        settings_response = client.post('/settings', json={'workspace_root': str(workspace_root)})
        assert settings_response.status_code == 200

        create_response = client.post('/jobs', json={'source_path': '/media/demo.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'running'
            db.commit()

        pause_response = client.post(f'/jobs/{job_id}/pause')
        assert pause_response.status_code == 200

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'paused'
            job.progress_percent = 42
            job.fps = 20.0
            job.eta_seconds = 120
            job.output_path = '/tmp/output.mkv'
            db.commit()

        workspace = workspace_root / str(job_id)
        workspace.mkdir(parents=True, exist_ok=True)
        partial = workspace / 'output.partial.mkv'
        partial.write_text('partial')

        resume_response = client.post(f'/jobs/{job_id}/resume')
        assert resume_response.status_code == 200
        assert resume_response.json()['status'] == 'queued'
        # Partial output is preserved on resume so the job can seek-resume instead
        # of re-encoding from the beginning.
        assert partial.exists()

        workspace.mkdir(parents=True, exist_ok=True)
        partial.write_text('partial')

        abort_response = client.post(f'/jobs/{job_id}/abort')
        assert abort_response.status_code == 200
        assert abort_response.json()['status'] == 'cancelled'
        assert abort_response.json()['error_message'] == 'Aborted by user'
        assert abort_response.json()['progress_percent'] == 0
        assert abort_response.json()['fps'] is None
        assert abort_response.json()['eta_seconds'] is None
        assert abort_response.json()['output_path'] is None
        assert not workspace.exists()

        pause_queue_response = client.post('/queue/pause')
        assert pause_queue_response.status_code == 200
        assert pause_queue_response.json() == {'status': 'paused'}

        paused_status_response = client.get('/queue/status')
        assert paused_status_response.status_code == 200
        assert paused_status_response.json() == {'status': 'paused'}

        resume_queue_response = client.post('/queue/resume')
        assert resume_queue_response.status_code == 200
        assert resume_queue_response.json() == {'status': 'running'}

        running_status_response = client.get('/queue/status')
        assert running_status_response.status_code == 200
        assert running_status_response.json() == {'status': 'running'}




def test_start_job_endpoint(monkeypatch, tmp_path):
    called = []

    from app.workers import queue as worker_queue

    monkeypatch.setattr(worker_queue, 'start_queued_job', lambda job_id, manual=False: (called.append((job_id, manual)) or (True, None)))

    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/start-me.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        response = client.post(f'/jobs/{job_id}/start')
        assert response.status_code == 200
        assert response.json()['id'] == job_id

    assert called == [(job_id, True)]




def test_start_job_endpoint_resumes_paused_job_before_start(monkeypatch):
    from app.core.database import SessionLocal
    from app.models.job import Job
    from app.workers import queue as worker_queue

    calls = []
    monkeypatch.setattr(worker_queue, 'start_queued_job', lambda job_id, manual=False: (calls.append((job_id, manual)) or (True, None)))

    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/start-paused.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'paused'
            db.commit()

        response = client.post(f'/jobs/{job_id}/start')
        assert response.status_code == 200

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.status == 'queued'

    assert calls == [(job_id, True)]

def test_start_job_endpoint_returns_reason_when_rejected(monkeypatch):
    from app.workers import queue as worker_queue

    monkeypatch.setattr(worker_queue, 'start_queued_job', lambda job_id, manual=False: (False, 'Maximum workers already running'))

    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/start-me-too.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        response = client.post(f'/jobs/{job_id}/start')
        assert response.status_code == 409
        assert response.json()['detail'] == 'Maximum workers already running'


def test_scan_endpoints_pause_queue_before_queueing(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'library'
    library_path.mkdir()

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    paused_reasons = []
    resumed_reasons = []
    from app.workers import queue as worker_queue

    monkeypatch.setattr(worker_queue, 'pause_queue', lambda reason='manual': paused_reasons.append(reason))
    monkeypatch.setattr(worker_queue, 'resume_queue', lambda reason='manual': resumed_reasons.append(reason))
    monkeypatch.setattr(routes, 'scan_enabled_libraries', lambda db: [])
    monkeypatch.setattr(
        routes,
        'scan_library',
        lambda db, library, include_disabled=False, progress_callback=None: [],
    )

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Scan Pause', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        lib_scan = client.post(f'/libraries/{library_id}/scan')
        assert lib_scan.status_code == 200

        scan_all = client.post('/scan')
        assert scan_all.status_code == 200

    assert paused_reasons == ['manual_scan', 'manual_scan']
    assert resumed_reasons == ['manual_scan', 'manual_scan']


def test_clear_queue_endpoint_removes_encode_and_download_items(monkeypatch):
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.job import Job

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.commit()

        encode = Job(input_path='/media/clear-me.mkv', status='queued', progress_percent=63, error_message='x')
        running_encode = Job(input_path='/media/clear-running.mkv', status='running', progress_percent=12)
        interrupted = Job(input_path='/media/clear-me-interrupted.mkv', status='interrupted')
        download = DownloadJob(
            source_file_path='/media/clear-me-download.mkv',
            status=DownloadJobStatus.searching.value,
            search_query='clear me',
            release_name='Release.Name',
            download_hash='deadbeef',
            client_type='qbittorrent',
            progress_percent=30,
        )
        db.add_all([encode, running_encode, interrupted, download])
        db.commit()
        db.refresh(encode)
        db.refresh(running_encode)
        db.refresh(interrupted)
        db.refresh(download)
        encode_id = encode.id
        running_encode_id = running_encode.id
        interrupted_id = interrupted.id
        download_id = download.id

    paused_reasons = []
    resumed_reasons = []
    stopped_job_ids = []
    from app.workers import queue as worker_queue
    monkeypatch.setattr(worker_queue, 'pause_queue', lambda reason='manual': paused_reasons.append(reason))
    monkeypatch.setattr(worker_queue, 'resume_queue', lambda reason='manual': resumed_reasons.append(reason))
    monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda job_id: stopped_job_ids.append(job_id) or True)

    with TestClient(app) as client:
        response = client.post('/queue/clear')
        assert response.status_code == 200
        payload = response.json()
        assert set(payload['removed_job_ids']) == {encode_id, running_encode_id, interrupted_id}
        assert payload['removed_download_job_ids'] == [download_id]

    with SessionLocal() as db:
        updated_encode = db.query(Job).filter(Job.id == encode_id).first()
        updated_running_encode = db.query(Job).filter(Job.id == running_encode_id).first()
        updated_interrupted = db.query(Job).filter(Job.id == interrupted_id).first()
        updated_download = db.query(DownloadJob).filter(DownloadJob.id == download_id).first()
        assert updated_encode is None
        assert updated_running_encode is None
        assert updated_interrupted is None
        assert updated_download is None

    assert stopped_job_ids == [encode_id, running_encode_id, interrupted_id]
    assert paused_reasons == ['manual']
    assert resumed_reasons == ['manual']


def test_discard_progress_reuses_terminal_download_job_for_search_instead(monkeypatch):
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.job import Job
    from app.models.library import Library, LibraryProfile
    from app.services import download_monitor_service, job_service, optimization_service

    source_path = '/media/The Testament of Ann Lee (2025).mkv'

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True)
        db.add(profile)
        db.commit()

        encode_job = Job(
            input_path=source_path,
            library_id=library.id,
            status='running',
            progress_percent=42,
        )
        db.add(encode_job)
        db.commit()
        db.refresh(encode_job)

        download_job = DownloadJob(
            library_id=library.id,
            source_file_path=source_path,
            status=DownloadJobStatus.waiting_encode.value,
            search_query='old query',
            release_name='Old.Release.Name',
            failed_release_keys='[\"guid:bad-guid\"]',
            retry_count=3,
            max_retries=5,
            download_hash='deadbeef',
            client_type='qbittorrent',
            progress_percent=100,
            encode_job_id=999,
        )
        db.add(download_job)
        db.commit()
        db.refresh(download_job)

        encode_job_id = encode_job.id
        download_job_id = download_job.id

    monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
    monkeypatch.setattr(download_monitor_service, 'create_download_job', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('create_download_job should not be called')))
    monkeypatch.setattr(job_service, '_get_settings', lambda _db: object())
    monkeypatch.setattr(optimization_service, 'stop_active_ffmpeg', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(optimization_service, 'delete_workspace', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('app.services.job_timing_service.stop_encode_timing', lambda *_args, **_kwargs: None)

    with TestClient(app) as client:
        response = client.post(f'/jobs/{encode_job_id}/discard-progress')
        assert response.status_code == 200
        payload = response.json()
        assert payload['status'] == 'cancelled'

    with SessionLocal() as db:
        updated_encode = db.query(Job).filter(Job.id == encode_job_id).first()
        updated_download = db.query(DownloadJob).filter(DownloadJob.id == download_job_id).first()

        assert updated_encode is not None
        assert updated_encode.status == 'cancelled'
        assert updated_encode.progress_percent == 0
        assert updated_encode.completed_at is not None

        assert updated_download is not None
        assert updated_download.status == DownloadJobStatus.pending.value
        assert updated_download.search_query is None
        assert updated_download.release_name is None
        assert updated_download.failed_release_keys is None
        assert updated_download.retry_count == 0
        assert updated_download.download_hash is None
        assert updated_download.client_type is None
        assert updated_download.progress_percent == 0
        assert updated_download.downloaded_file_path is None
        assert updated_download.imported_file_path is None
        assert updated_download.encode_job_id == encode_job_id


def test_recovery_endpoint_marks_interrupted_requeues_and_cleans_workspace(tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    workspace_root = tmp_path / 'workspaces'

    with TestClient(app) as client:
        settings_response = client.post(
            '/settings',
            json={
                'workspace_root': str(workspace_root),
                'requeue_interrupted_jobs': True,
                'cleanup_workspaces_on_startup': True,
            },
        )
        assert settings_response.status_code == 200

        create_response = client.post('/jobs', json={'source_path': '/media/recover-me.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'running'
            job.progress_percent = 61
            job.fps = 24.0
            db.commit()

        workspace = workspace_root / str(job_id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'output.partial.mkv').write_text('partial')

        recovery_response = client.post('/recovery/run')
        assert recovery_response.status_code == 200
        summary = recovery_response.json()
        assert summary['recovered_jobs'] >= 1
        assert summary['requeued_jobs'] >= 1
        assert summary['cleaned_workspaces'] >= 1

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.status == 'queued'
            assert job.progress_percent == 0
            assert job.fps is None

        assert not workspace.exists()


def test_recovery_endpoint_can_keep_workspace_and_not_requeue(tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    workspace_root = tmp_path / 'workspaces'

    with TestClient(app) as client:
        settings_response = client.post(
            '/settings',
            json={
                'workspace_root': str(workspace_root),
                'requeue_interrupted_jobs': False,
                'cleanup_workspaces_on_startup': False,
                'min_free_gb': 32,
            },
        )
        assert settings_response.status_code == 200

        create_response = client.post('/jobs', json={'source_path': '/media/keep-workspace.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'preflight'
            db.commit()

        workspace = workspace_root / str(job_id)
        workspace.mkdir(parents=True, exist_ok=True)
        marker = workspace / 'marker.txt'
        marker.write_text('keep')

        recovery_response = client.post('/recovery/run')
        assert recovery_response.status_code == 200
        summary = recovery_response.json()
        assert summary['recovered_jobs'] >= 1
        assert summary['requeued_jobs'] == 0
        assert summary['cleaned_workspaces'] == 0

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.status == 'interrupted'

        assert marker.exists()




def test_cleanup_endpoint_removes_stale_workspaces_only(tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    workspace_root = tmp_path / 'workspaces'

    with TestClient(app) as client:
        settings_response = client.post('/settings', json={'workspace_root': str(workspace_root)})
        assert settings_response.status_code == 200

        running_response = client.post('/jobs', json={'source_path': '/media/running-cleanup.mkv'})
        stale_response = client.post('/jobs', json={'source_path': '/media/stale-cleanup.mkv'})
        assert running_response.status_code == 201
        assert stale_response.status_code == 201
        running_id = running_response.json()['id']
        stale_id = stale_response.json()['id']

        with SessionLocal() as db:
            running_job = db.query(Job).filter(Job.id == running_id).first()
            stale_job = db.query(Job).filter(Job.id == stale_id).first()
            assert running_job is not None
            assert stale_job is not None
            running_job.status = 'running'
            stale_job.status = 'failed'
            db.commit()

        running_workspace = workspace_root / str(running_id)
        stale_workspace = workspace_root / str(stale_id)
        orphan_workspace = workspace_root / '999999'
        running_workspace.mkdir(parents=True, exist_ok=True)
        stale_workspace.mkdir(parents=True, exist_ok=True)
        orphan_workspace.mkdir(parents=True, exist_ok=True)

        cleanup_response = client.post('/cleanup/run')
        assert cleanup_response.status_code == 200
        summary = cleanup_response.json()
        assert summary['cleaned_workspaces'] == 2

        assert running_workspace.exists()
        assert not stale_workspace.exists()
        assert not orphan_workspace.exists()


def test_abort_all_jobs_endpoint():
    from app.core.database import SessionLocal
    from app.models.job import Job

    with TestClient(app) as client:
        first = client.post('/jobs', json={'source_path': '/media/a.mkv'})
        second = client.post('/jobs', json={'source_path': '/media/b.mkv'})
        assert first.status_code == 201
        assert second.status_code == 201

        with SessionLocal() as db:
            jobs = db.query(Job).filter(Job.id.in_([first.json()['id'], second.json()['id']])).all()
            for job in jobs:
                job.progress_percent = 55
                job.fps = 29.97
                job.eta_seconds = 45
                job.output_path = '/tmp/old-output.mkv'
            db.commit()

        response = client.post('/jobs/abort-all')
        assert response.status_code == 200
        payload = response.json()
        assert len(payload['aborted_job_ids']) >= 2

        with SessionLocal() as db:
            jobs = db.query(Job).filter(Job.id.in_(payload['aborted_job_ids'])).all()
            assert len(jobs) == len(payload['aborted_job_ids'])
            for job in jobs:
                assert job.status == 'cancelled'
                assert job.error_message == 'Aborted by user'
                assert job.progress_percent == 0
                assert job.fps is None
                assert job.eta_seconds is None
                assert job.output_path is None




def test_remove_all_jobs_endpoint_removes_only_terminal_jobs():
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob
    from app.models.job import Job

    with TestClient(app) as client:
        complete = client.post('/jobs', json={'source_path': '/media/complete-remove.mkv'})
        queued = client.post('/jobs', json={'source_path': '/media/queued-keep.mkv'})
        assert complete.status_code == 201
        assert queued.status_code == 201

        complete_id = complete.json()['id']
        queued_id = queued.json()['id']

        with SessionLocal() as db:
            complete_job = db.query(Job).filter(Job.id == complete_id).first()
            queued_job = db.query(Job).filter(Job.id == queued_id).first()
            assert complete_job is not None
            assert queued_job is not None
            complete_job.status = 'skipped'
            download_job = DownloadJob(
                library_id=None,
                source_file_path='/downloads/complete-remove.mkv',
                status='failed',
                progress_percent=100,
            )
            db.add(download_job)
            db.commit()
            download_job_id = download_job.id

        response = client.post('/jobs/remove-all')
        assert response.status_code == 200
        payload = response.json()
        assert complete_id in payload['removed_job_ids']
        assert queued_id not in payload['removed_job_ids']
        assert download_job_id in payload['removed_download_job_ids']

        with SessionLocal() as db:
            assert db.query(Job).filter(Job.id == complete_id).first() is None
            assert db.query(Job).filter(Job.id == queued_id).first() is not None
            assert db.query(DownloadJob).filter(DownloadJob.id == download_job_id).first() is None

        cleanup = client.post('/jobs/abort-all')
        assert cleanup.status_code == 200
        second_pass = client.post('/jobs/remove-all')
        assert second_pass.status_code == 200

def test_get_encoders_endpoint(monkeypatch):
    monkeypatch.setattr(routes, 'require_ui_auth', lambda credentials=None: None)
    monkeypatch.setattr('app.services.optimization_service.available_encoders_by_codec', lambda: {
        'h264': ['h264_qsv', 'libx264'],
        'hevc': ['hevc_qsv'],
        'av1': ['libsvtav1'],
    })

    with TestClient(app) as client:
        response = client.get('/encoders')
        assert response.status_code == 200
        data = response.json()
        assert len(data['encoders']) == 3
        h264 = next(item for item in data['encoders'] if item['codec'] == 'h264')
        assert h264['available_encoders'] == ['h264_qsv', 'libx264']


def test_cleanup_optimized_endpoint_preserves_single_recorded_output(tmp_path):
    from app.core.database import SessionLocal
    from app.models.event_log import EventLog
    from app.models.job import Job

    output_file = tmp_path / 'movie-opt.mkv'
    output_file.write_text('optimized')

    with TestClient(app) as client:
        created = client.post('/jobs', json={'source_path': '/media/source-hdr.mkv'})
        assert created.status_code == 201
        job_id = created.json()['id']

        with SessionLocal() as db:
            db.query(EventLog).delete()
            db.commit()
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'complete'
            job.output_path = str(output_file)
            db.commit()

        cleanup_response = client.post('/cleanup/optimized')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert job_id not in payload['affected_job_ids']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            assert job.output_path == str(output_file)

        logs_response = client.get('/logs')
        assert logs_response.status_code == 200
        logs = logs_response.json()
        assert logs[0]['event_type'] == 'optimized_cleanup_summary'
        assert logs[0]['details']['deleted_files'] == 0
        assert job_id not in logs[0]['details']['affected_job_ids']
        assert logs[1]['event_type'] == 'optimized_cleanup_started'
        assert logs[1]['message'] == 'Optimized output cleanup started'

    assert output_file.exists()


def test_clear_logs_endpoint_deletes_event_logs():
    from app.core.database import SessionLocal
    from app.models.event_log import EventLog
    from app.services import event_log_service

    with TestClient(app) as client:
        with SessionLocal() as db:
            db.query(EventLog).delete()
            db.commit()
            event_log_service.record_event(db, 'one', 'First event')
            event_log_service.record_event(db, 'two', 'Second event')

        response = client.delete('/logs')
        assert response.status_code == 200
        assert response.json() == {'deleted_logs': 2}

        with SessionLocal() as db:
            assert db.query(EventLog).count() == 0

        logs_response = client.get('/logs')
        assert logs_response.status_code == 200
        assert logs_response.json() == []


def test_cleanup_duplicate_optimized_endpoint_preserves_unrecorded_versioned_outputs(tmp_path):
    from app.core.database import SessionLocal
    from app.models.event_log import EventLog
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_file = library_path / 'movie.mkv'
    canonical_output = library_path / 'movie-1080p.mkv'
    duplicate_v2 = library_path / 'movie-1080p-v2.mkv'
    duplicate_v10 = library_path / 'movie-1080p-v10.mkv'
    wrong_container = library_path / 'movie-1080p-v3.mp4'
    for path in [source_file, canonical_output, duplicate_v2, duplicate_v10, wrong_container]:
        path.write_text(path.name)

    with SessionLocal() as db:
        db.query(EventLog).delete()
        db.commit()
        library = Library(name='Duplicate Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id))
        db.commit()
        library_id = library.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert payload['affected_library_ids'] == []

        logs_response = client.get('/logs')
        assert logs_response.status_code == 200
        logs = logs_response.json()
        assert logs[0]['event_type'] == 'duplicate_optimized_cleanup_summary'
        assert logs[0]['details']['affected_library_names'] == []
        assert 'affected_library_ids' not in logs[0]['details']
        assert logs[1]['event_type'] == 'duplicate_optimized_cleanup_started'

    assert source_file.exists()
    assert canonical_output.exists()
    assert duplicate_v2.exists()
    assert duplicate_v10.exists()
    assert wrong_container.exists()


def test_cleanup_duplicate_optimized_endpoint_keeps_2k_sibling_when_canonical_exists(tmp_path):
    from app.core.database import SessionLocal
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_file = library_path / 'Movie Title (2024) 2160p HDR.mkv'
    canonical_output = library_path / 'Movie Title (2024)-1080p.mkv'
    duplicate_2k = library_path / 'Movie Title (2024) 2K.mkv'
    for path in [source_file, canonical_output, duplicate_2k]:
        path.write_text(path.name)

    with SessionLocal() as db:
        library = Library(name='Duplicate 2K Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.commit()
        library_id = library.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert payload['affected_library_ids'] == []

    assert source_file.exists()
    assert canonical_output.exists()
    assert duplicate_2k.exists()


def test_cleanup_duplicate_optimized_endpoint_preserves_unrecorded_1080p_filesystem_versions(tmp_path):
    from app.core.database import SessionLocal
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_4k = library_path / 'Movie Title (2024) 2160p HDR.mkv'
    larger_1080p = library_path / 'Movie Title (2024) 1080p 2.6Mbps.mkv'
    smaller_1080p = library_path / 'Movie Title (2024) 1080p 2.1Mbps.mkv'
    source_4k.write_text('4k-source')
    larger_1080p.write_text('larger-optimized-version')
    smaller_1080p.write_text('small')

    with SessionLocal() as db:
        library = Library(name='Duplicate Plex Versions', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.commit()
        library_id = library.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert payload['affected_library_ids'] == []

    assert source_4k.exists()
    assert larger_1080p.exists()
    assert smaller_1080p.exists()


def test_cleanup_duplicate_optimized_endpoint_preserves_unrecorded_4k_filesystem_versions(tmp_path):
    from app.core.database import SessionLocal
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    stale_larger_4k = library_path / 'Movie Title (2024) 2160p Remux.mkv'
    current_smaller_4k = library_path / 'Movie Title (2024) 4K WEB-DL.mkv'
    optimized_1080p = library_path / 'Movie Title (2024)-1080p.mkv'
    stale_larger_4k.write_text('larger-original-4k-version')
    current_smaller_4k.write_text('small-4k')
    optimized_1080p.write_text('optimized-1080p')
    os.utime(stale_larger_4k, (1_600_000_000, 1_600_000_000))
    os.utime(current_smaller_4k, (1_700_000_000, 1_700_000_000))

    with SessionLocal() as db:
        library = Library(name='Duplicate Plex 4K Versions', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.commit()
        library_id = library.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert payload['affected_library_ids'] == []

    assert stale_larger_4k.exists()
    assert current_smaller_4k.exists()
    assert optimized_1080p.exists()


def test_cleanup_duplicate_optimized_endpoint_preserves_single_4k_original_with_1080p_output(tmp_path):
    from datetime import UTC, datetime

    from app.core.database import SessionLocal
    from app.models.job import Job
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_4k = library_path / 'Movie Title (2024) 2160p HDR.mkv'
    optimized_1080p = library_path / 'Movie Title (2024)-1080p.mkv'
    source_4k.write_text('single-original-4k-file')
    optimized_1080p.write_text('optimized-1080p-output')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Radarr Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.add(Job(
            input_path=str(source_4k),
            output_path=str(optimized_1080p),
            status='complete',
            library_id=library.id,
            completed_at=datetime.now(UTC),
        ))
        db.commit()

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 0
        assert payload['affected_library_ids'] == []

    assert source_4k.exists()
    assert optimized_1080p.exists()


def test_cleanup_duplicate_optimized_endpoint_keeps_4k_and_one_optimized_floor(tmp_path):
    from datetime import UTC, datetime

    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.job import Job
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_4k = library_path / 'Movie Title (2024) 2160p HDR.mkv'
    imported_4k = library_path / 'Movie Title (2024) 2160p Remux.mkv'
    encoded_1080p = library_path / 'Movie Title (2024)-1080p.mkv'
    imported_1080p = library_path / 'Movie Title (2024) 1080p WEB-DL.mkv'
    source_4k.write_text('source-4k')
    imported_4k.write_text('recorded-4k-artifact')
    encoded_1080p.write_text('small')
    imported_1080p.write_text('larger-optimized-artifact')

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Radarr Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.add(Job(
            input_path=str(source_4k),
            output_path=str(encoded_1080p),
            status='complete',
            library_id=library.id,
            completed_at=datetime.now(UTC),
        ))
        db.add_all([
            DownloadJob(
                library_id=library.id,
                source_file_path=str(source_4k),
                status=DownloadJobStatus.complete.value,
                imported_file_path=str(imported_4k),
                completed_at=datetime.now(UTC),
            ),
            DownloadJob(
                library_id=library.id,
                source_file_path=str(source_4k),
                status=DownloadJobStatus.complete.value,
                imported_file_path=str(imported_1080p),
                completed_at=datetime.now(UTC),
            ),
        ])
        db.commit()
        library_id = library.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 1
        assert payload['affected_library_ids'] == [library_id]

    assert source_4k.exists()
    assert imported_4k.exists()
    assert not encoded_1080p.exists()
    assert imported_1080p.exists()


def test_cleanup_duplicate_optimized_endpoint_deletes_recorded_duplicate_artifacts_across_labels(tmp_path):
    from datetime import UTC, datetime

    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.job import Job
    from app.models.library import Library, LibraryProfile

    library_path = tmp_path / 'movies'
    library_path.mkdir()
    source_file = library_path / 'Movie Title (2024) 2160p HDR.mkv'
    encoded_2k = library_path / 'Movie Title (2024)-1080p.mkv'
    downloaded_1080p = library_path / 'Movie Title (2024) 1080p WEB-DL.mkv'
    for path in [source_file, encoded_2k, downloaded_1080p]:
        path.write_text(path.name)

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Recorded Duplicate Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id, target_resolution=1080))
        db.commit()
        encode_job = Job(
            input_path=str(source_file),
            output_path=str(encoded_2k),
            status='complete',
            library_id=library.id,
            completed_at=datetime.now(UTC),
        )
        download_job = DownloadJob(
            library_id=library.id,
            source_file_path=str(source_file),
            status=DownloadJobStatus.complete.value,
            imported_file_path=str(downloaded_1080p),
            completed_at=datetime.now(UTC),
        )
        db.add_all([encode_job, download_job])
        db.commit()
        library_id = library.id
        encode_job_id = encode_job.id
        download_job_id = download_job.id

    with TestClient(app) as client:
        cleanup_response = client.post('/cleanup/optimized/duplicates')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] == 1
        assert payload['affected_library_ids'] == [library_id]

    with SessionLocal() as db:
        encode_job = db.query(Job).filter(Job.id == encode_job_id).first()
        download_job = db.query(DownloadJob).filter(DownloadJob.id == download_job_id).first()
        assert encode_job.output_path is None
        assert download_job.imported_file_path == str(downloaded_1080p)

    assert source_file.exists()
    assert not encoded_2k.exists()
    assert downloaded_1080p.exists()


def test_download_history_delete_file_preserves_qbit_seed(tmp_path, monkeypatch):
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.services import download_client_service

    library_dir = tmp_path / 'library'
    seed_dir = tmp_path / 'qbit'
    library_dir.mkdir()
    seed_dir.mkdir()
    source = library_dir / 'When Harry Met Sally (1989).mkv'
    source.write_bytes(b'original')
    seed_file = seed_dir / 'When.Harry.Met.Sally.1989.1080p.mkv'
    seed_file.write_bytes(b'download')
    imported = library_dir / 'When Harry Met Sally (1989)-1080p.mkv'
    os.link(seed_file, imported)

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()
        library = Library(name='Movies', path=str(library_dir), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id))
        dj = DownloadJob(
            library_id=library.id,
            source_file_path=str(source),
            imported_file_path=str(imported),
            downloaded_file_path=str(seed_file),
            download_hash='seed-me',
            client_type='qbittorrent',
            status=DownloadJobStatus.complete.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)
        job_id = dj.id

    monkeypatch.setattr(
        download_client_service,
        'remove_qbt_torrent',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('qBit torrent must be retained')),
    )
    with TestClient(app) as client:
        response = client.post(f'/download-jobs/{job_id}/delete-file', json={'retry': False})

    assert response.status_code == 200
    assert response.json()['status'] == DownloadJobStatus.file_deleted.value
    assert not imported.exists()
    assert seed_file.exists()
    assert seed_file.read_bytes() == b'download'


def test_download_history_delete_and_retry_keeps_seed_and_queues_search(tmp_path, monkeypatch):
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.services import download_client_service

    library_dir = tmp_path / 'library'
    seed_dir = tmp_path / 'qbit'
    library_dir.mkdir()
    seed_dir.mkdir()
    source = library_dir / 'When Harry Met Sally (1989).mkv'
    source.write_bytes(b'original')
    seed_file = seed_dir / 'When.Harry.Met.Sally.1989.1080p.mkv'
    seed_file.write_bytes(b'download')
    imported = library_dir / 'When Harry Met Sally (1989)-1080p.mkv'
    os.link(seed_file, imported)

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()
        library = Library(name='Movies', path=str(library_dir), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)
        db.add(LibraryProfile(library_id=library.id))
        dj = DownloadJob(
            library_id=library.id,
            source_file_path=str(source),
            imported_file_path=str(imported),
            downloaded_file_path=str(seed_file),
            release_name='When.Harry.Met.Sally.1989.1080p.WEB-DL',
            selected_release_key='guid:first-release',
            download_hash='seed-me',
            client_type='qbittorrent',
            status=DownloadJobStatus.complete.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)
        job_id = dj.id

    monkeypatch.setattr(
        download_client_service,
        'remove_qbt_torrent',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('qBit torrent must be retained')),
    )
    with TestClient(app) as client:
        response = client.post(f'/download-jobs/{job_id}/delete-file', json={'retry': True})

    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == DownloadJobStatus.pending.value
    assert payload['download_hash'] is None
    assert 'guid:first-release' in json.loads(payload['failed_release_keys'])
    assert not imported.exists()
    assert seed_file.exists()
