from types import SimpleNamespace

from app.services import download_client_service


def test_get_sab_status_parses_timeleft_eta_and_speed(monkeypatch):
    queue_payload = {
        'queue': {
            'kbpersec': '1024',
            'slots': [
                {
                    'nzo_id': 'NZO123',
                    'percentage': '55',
                    'status': 'Downloading',
                    'timeleft': '0:03:10',
                }
            ],
        }
    }

    monkeypatch.setattr(download_client_service, '_sab_api', lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}})
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO123')

    assert status['progress_percent'] == 55
    assert status['eta_seconds'] == 190
    assert status['download_speed_bps'] == 1024 * 1024
    assert status['client_queue_position'] == 0
    assert status['is_complete'] is False
    assert status['not_found'] is False


def test_set_sab_category_uses_change_cat_first(monkeypatch):
    calls = []

    def fake_sab_api(_settings, **params):
        calls.append(params)
        return {'status': True}

    monkeypatch.setattr(download_client_service, '_sab_api', fake_sab_api)

    ok = download_client_service.set_sab_category(SimpleNamespace(), 'NZO456', category='optimizarr')

    assert ok is True
    assert calls
    assert calls[0].get('mode') == 'change_cat'
    assert calls[0].get('value') == 'NZO456'
    assert calls[0].get('value2') == 'optimizarr'


def test_get_sab_status_prefers_mbps_over_mb_size_field(monkeypatch):
    queue_payload = {
        'queue': {
            'kbpersec': '1024',
            'slots': [
                {
                    'nzo_id': 'NZO999',
                    'percentage': '40',
                    'status': 'Downloading',
                    'timeleft': '0:01:10',
                    'mb': '8.05',
                    'mbps': '67',
                }
            ],
        }
    }

    monkeypatch.setattr(download_client_service, '_sab_api', lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}})
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO999')

    assert status['download_speed_bps'] == 67 * 1024 * 1024


def test_get_qbt_status_marks_moving_state(monkeypatch):
    monkeypatch.setattr(download_client_service, '_qbt_session', lambda _s: object())
    monkeypatch.setattr(
        download_client_service,
        '_qbt_torrent_info',
        lambda _client, _hash: {
            'state': 'moving',
            'progress': 0,
            'eta': -1,
            'dlspeed': 0,
            'content_path': '/downloads/complete/Movie.2025.1080p',
        },
    )

    status = download_client_service.get_qbt_status(SimpleNamespace(), 'abc123')

    assert status['is_moving'] is True
    assert status['is_complete'] is False
    assert status['not_found'] is False


def test_get_qbt_status_marks_queued_download_as_waiting(monkeypatch):
    monkeypatch.setattr(download_client_service, '_qbt_session', lambda _s: object())
    monkeypatch.setattr(
        download_client_service,
        '_qbt_torrent_info',
        lambda _client, _hash: {
            'state': 'queuedDL',
            'progress': 0,
            'eta': 8640000,
            'dlspeed': 0,
            'content_path': '/downloads/incomplete/Queued.Movie.2025.1080p',
        },
    )

    status = download_client_service.get_qbt_status(SimpleNamespace(), 'abc123')

    assert status['is_waiting'] is True
    assert status['qbt_state'] == 'queuedDL'
    assert status['is_stalled'] is False
    assert status['is_complete'] is False
    assert status['not_found'] is False


def test_qbt_session_accepts_204_login_success(monkeypatch):
    posts = []

    class Response:
        status_code = 204
        text = ''

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, path, data=None):
            posts.append((path, data))
            return Response()

    monkeypatch.setattr(download_client_service.httpx, 'Client', Client)
    monkeypatch.setattr(download_client_service.secrets_store, 'decrypt_secret', lambda value: value)

    settings = SimpleNamespace(host='http://qbit.local', port=8080, username='user', password='pass')

    client = download_client_service._qbt_session(settings)

    assert isinstance(client, Client)
    assert posts == [('/api/v2/auth/login', {'username': 'user', 'password': 'pass'})]


def test_get_sab_status_marks_moving_state(monkeypatch):
    queue_payload = {
        'queue': {
            'slots': [
                {
                    'nzo_id': 'NZO-MOVE',
                    'percentage': '0',
                    'status': 'Moving',
                    'timeleft': '0',
                }
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-MOVE')

    assert status['is_moving'] is True
    assert status['is_complete'] is False
    assert status['not_found'] is False


def test_get_sab_status_marks_queued_status_as_waiting(monkeypatch):
    queue_payload = {
        'queue': {
            'kbpersec': '1234',
            'mbps': '67',
            'timeleft': '0:01:30',
            'slots': [
                {
                    'nzo_id': 'NZO-QUEUED',
                    'percentage': '0',
                    'status': 'Queued',
                    'timeleft': '-',
                }
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-QUEUED')

    assert status['is_waiting'] is True
    assert status['sab_status'] == 'Queued'
    assert status['eta_seconds'] is None
    assert status['download_speed_bps'] == 0
    assert status['is_stalled'] is False
    assert status['is_complete'] is False
    assert status['not_found'] is False


def test_get_sab_status_marks_queue_wide_pause_as_paused_for_active_slot(monkeypatch):
    queue_payload = {
        'queue': {
            'paused': True,
            'kbpersec': '0',
            'slots': [
                {
                    'nzo_id': 'NZO-QPAUSE',
                    'percentage': '24',
                    'status': 'Downloading',
                    'timeleft': '0:12:00',
                }
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-QPAUSE')

    assert status['is_waiting'] is True
    assert str(status['sab_status']).strip().lower() == 'paused'
    assert status['progress_percent'] == 24
    assert status['eta_seconds'] is None
    assert status['download_speed_bps'] == 0
    assert status['is_stalled'] is False
    assert status['is_moving'] is False
    assert status['not_found'] is False


def test_get_sab_status_queue_wide_pause_does_not_override_moving_slot(monkeypatch):
    queue_payload = {
        'queue': {
            'paused': True,
            'slots': [
                {
                    'nzo_id': 'NZO-QPAUSE-MOVE',
                    'percentage': '100',
                    'status': 'Moving',
                    'timeleft': '0',
                }
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-QPAUSE-MOVE')

    assert status['is_moving'] is True
    assert status['sab_status'] == 'Moving'
    assert status['is_waiting'] is False


def test_get_sab_status_queue_wide_pause_does_not_override_stalled_slot(monkeypatch):
    queue_payload = {
        'queue': {
            'paused': True,
            'slots': [
                {
                    'nzo_id': 'NZO-QPAUSE-STALL',
                    'percentage': '10',
                    'status': 'Stalled',
                    'timeleft': '0',
                }
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-QPAUSE-STALL')

    assert status['is_stalled'] is True
    assert status['sab_status'] == 'Stalled'


def test_get_sab_status_no_queue_wide_pause_leaves_active_slot_downloading(monkeypatch):
    queue_payload = {
        'queue': {
            'paused': False,
            'kbpersec': '512',
            'slots': [
                {
                    'nzo_id': 'NZO-ACTIVE-OK',
                    'percentage': '24',
                    'status': 'Downloading',
                    'timeleft': '0:12:00',
                }
            ],
        }
    }
    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-ACTIVE-OK')

    assert status['is_waiting'] is False
    assert status['sab_status'] == 'Downloading'
    assert status['eta_seconds'] == 720


def test_get_sab_status_does_not_apply_global_speed_to_later_queue_slot(monkeypatch):
    queue_payload = {
        'queue': {
            'kbpersec': '3400',
            'timeleft': '3:26:00',
            'slots': [
                {
                    'nzo_id': 'NZO-ACTIVE',
                    'percentage': '42',
                    'status': 'Downloading',
                    'timeleft': '0:22:00',
                },
                {
                    'nzo_id': 'NZO-WAITING',
                    'percentage': '0',
                    'status': 'Downloading',
                    'timeleft': '3:26:00',
                    'kbpersec': '3400',
                },
            ],
        }
    }

    monkeypatch.setattr(
        download_client_service,
        '_sab_api',
        lambda *_args, **params: queue_payload if params.get('mode') == 'queue' else {'history': {'slots': []}},
    )
    status = download_client_service.get_sab_status(SimpleNamespace(), 'NZO-WAITING')

    assert status['is_waiting'] is True
    assert status['client_queue_position'] == 1
    assert status['eta_seconds'] is None
    assert status['download_speed_bps'] == 0
    assert status['is_complete'] is False
    assert status['not_found'] is False
