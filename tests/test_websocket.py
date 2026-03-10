import time

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models.auth import AdminUser, AuthSession
from app.models.job import Job
from app.services.realtime_service import RealtimeBroker
from app.services import auth_service

BOOTSTRAP_TOKEN = 'test-bootstrap-token'



def _clear_jobs():
    with SessionLocal() as db:
        db.query(Job).delete()
        db.commit()


def _clear_auth_state():
    with SessionLocal() as db:
        db.query(AuthSession).delete()
        db.query(AdminUser).delete()
        db.commit()

def _receive_event_of_type(websocket, event_type: str, max_messages: int = 20) -> dict:
    for _ in range(max_messages):
        payload = websocket.receive_json()
        if payload.get('type') == event_type:
            return payload
    raise AssertionError(f'Expected event type {event_type}')


def test_ws_stream_receives_job_update():
    _clear_jobs()
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as websocket:
            create_response = client.post('/jobs', json={'source_path': '/media/ws-demo.mkv'})
            assert create_response.status_code == 201
            created_job = create_response.json()

            event = _receive_event_of_type(websocket, 'job_update')
            assert event['data']['id'] == created_job['id']
            assert event['data']['source_path'] == '/media/ws-demo.mkv'


def test_ws_requires_session_when_admin_configured():
    _clear_jobs()
    _clear_auth_state()

    with TestClient(app) as client:
        secret = client.post('/auth/totp/secret', json={'username': 'admin'}).json()['secret']
        bootstrap = client.post(
            '/auth/bootstrap',
            json={
                'username': 'admin',
                'bootstrap_token': BOOTSTRAP_TOKEN,
                'password': 'VeryStrongPassword123',
                'enable_two_factor': True,
                'totp_secret': secret,
                'totp_code': auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert bootstrap.status_code == 201
        client.post('/auth/logout', headers={'X-CSRF-Token': client.cookies.get('optimizarr_csrf', '')})

        try:
            with client.websocket_connect('/ws'):
                raise AssertionError('expected websocket auth failure')
        except Exception:
            pass

        login = client.post(
            '/auth/login',
            json={
                'username': 'admin',
                'password': 'VeryStrongPassword123',
                'otp_code': auth_service.current_totp_code(secret, at_time=int(time.time())),
            },
        )
        assert login.status_code == 200

        with client.websocket_connect('/ws') as websocket:
            create_response = client.post(
                '/jobs',
                json={'source_path': '/media/ws-auth-demo.mkv'},
                headers={'X-CSRF-Token': client.cookies.get('optimizarr_csrf', '')},
            )
            assert create_response.status_code == 201

            event = _receive_event_of_type(websocket, 'job_update')
            assert event['data']['source_path'] == '/media/ws-auth-demo.mkv'

    _clear_auth_state()


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
        'encode_duration_seconds': None,
    }

    broker.publish_job_update(payload, throttle_progress=True)
    broker.publish_job_update(payload, throttle_progress=True)
    assert subscription.queue.qsize() == 1

    broker.unsubscribe(subscription.client_id)




def test_job_progress_payload_includes_completed_at_field():
    broker = RealtimeBroker()
    subscription = broker.subscribe()

    payload = {
        'id': 102,
        'status': 'queued',
        'source_path': '/media/completed-at.mkv',
        'output_path': None,
        'retry_count': 0,
        'cancel_requested': False,
        'progress_percent': 0,
        'fps': None,
        'eta_seconds': None,
        'encoder_used': None,
        'codec_used': None,
        'hwaccel_used': None,
        'used_fallback': None,
        'fallback_reason': None,
        'error_message': None,
        'encode_duration_seconds': None,
        'completed_at': None,
    }

    broker.publish_job_update(payload, throttle_progress=False)
    event = subscription.queue.get(timeout=1)

    assert event['type'] == 'job_update'
    assert 'completed_at' in event['data']
    assert 'encode_duration_seconds' in event['data']
    assert event['data']['encode_duration_seconds'] is None
    assert event['data']['completed_at'] is None

    broker.unsubscribe(subscription.client_id)

def test_ws_stream_receives_queue_pause_system_event():
    with TestClient(app) as client:
        client.post('/queue/resume')
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
