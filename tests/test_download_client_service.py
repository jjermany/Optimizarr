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
