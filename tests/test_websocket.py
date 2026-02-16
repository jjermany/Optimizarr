from fastapi.testclient import TestClient

from app.main import app
from app.services.realtime_service import RealtimeBroker


def _receive_event_of_type(websocket, event_type: str, max_messages: int = 20) -> dict:
    for _ in range(max_messages):
        payload = websocket.receive_json()
        if payload.get('type') == event_type:
            return payload
    raise AssertionError(f'Expected event type {event_type}')


def test_ws_stream_receives_job_update():
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as websocket:
            create_response = client.post('/jobs', json={'source_path': '/media/ws-demo.mkv'})
            assert create_response.status_code == 201
            created_job = create_response.json()

            event = _receive_event_of_type(websocket, 'job_update')
            assert event['data']['id'] == created_job['id']
            assert event['data']['source_path'] == '/media/ws-demo.mkv'


def test_ws_requires_token_when_basic_auth_enabled(monkeypatch):
    monkeypatch.setenv('OPTIMIZARR_UI_USERNAME', 'admin')
    monkeypatch.setenv('OPTIMIZARR_UI_PASSWORD', 'secret')

    with TestClient(app) as client:
        try:
            with client.websocket_connect('/ws'):
                raise AssertionError('expected websocket auth failure')
        except Exception:
            pass

        token_response = client.get('/auth/ws-token', auth=('admin', 'secret'))
        assert token_response.status_code == 200
        token = token_response.json()['token']

        with client.websocket_connect(f'/ws?token={token}') as websocket:
            create_response = client.post('/jobs', json={'source_path': '/media/ws-auth-demo.mkv'}, auth=('admin', 'secret'))
            assert create_response.status_code == 201

            event = _receive_event_of_type(websocket, 'job_update')
            assert event['data']['source_path'] == '/media/ws-auth-demo.mkv'


def test_job_progress_throttle_limits_to_one_event_per_second():
    broker = RealtimeBroker()
    subscription = broker.subscribe()

    payload = {
        'id': 101,
        'status': 'running',
        'source_path': '/media/progress.mkv',
        'output_path': None,
        'retry_count': 0,
        'cancel_requested': False,
        'progress_percent': 10,
        'fps': 1.5,
        'eta_seconds': 10,
        'encoder_used': None,
        'codec_used': None,
        'hwaccel_used': None,
        'used_fallback': None,
        'fallback_reason': None,
        'error_message': None,
    }

    broker.publish_job_update(payload, throttle_progress=True)
    broker.publish_job_update(payload, throttle_progress=True)
    assert subscription.queue.qsize() == 1

    broker.unsubscribe(subscription.client_id)


def test_ws_stream_receives_queue_pause_system_event():
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as websocket:
            pause_response = client.post('/queue/pause')
            assert pause_response.status_code == 200

            event = _receive_event_of_type(websocket, 'system_event')
            while event['data'].get('event') != 'queue_paused':
                event = _receive_event_of_type(websocket, 'system_event')
            assert event['data']['reason'] == 'manual'

            client.post('/queue/resume')


def test_realtime_broker_publish_system_event():
    broker = RealtimeBroker()
    subscription = broker.subscribe()

    broker.publish_system_event('job_aborted', job_id=77)
    event = subscription.queue.get(timeout=1)

    assert event['type'] == 'system_event'
    assert event['data']['event'] == 'job_aborted'
    assert event['data']['job_id'] == 77

    broker.unsubscribe(subscription.client_id)
