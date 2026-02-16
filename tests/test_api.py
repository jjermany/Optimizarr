import time

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get('/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


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


def test_scan_cancel_and_retry_endpoints():
    with TestClient(app) as client:
        scan_response = client.post('/jobs/scan', json={'source_paths': ['/media/a.mkv', '/media/b.mkv']})
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 2

        target_job_id = created_jobs[0]['id']

        cancel_response = client.post(f'/jobs/{target_job_id}/cancel')
        assert cancel_response.status_code == 200
        cancelled = cancel_response.json()
        assert cancelled['status'] in {'cancelled', 'running'}

        retry_response = client.post(f'/jobs/{target_job_id}/retry')
        assert retry_response.status_code == 200
        retried = retry_response.json()
        assert retried['status'] in {'queued', 'cancelled', 'running'}
