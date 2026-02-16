import time

from fastapi.testclient import TestClient

from app.api import routes
from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get('/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


def test_version_endpoint():
    with TestClient(app) as client:
        response = client.get('/version')

    assert response.status_code == 200
    assert 'version' in response.json()


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
    with TestClient(app) as client:
        get_response = client.get('/settings')
        assert get_response.status_code == 200
        payload = get_response.json()
        assert 'enable_optimizer' in payload
        assert 'schedule_start_hour' in payload
        assert 'history_retention_days' in payload

        update_response = client.post(
            '/settings',
            json={
                'enable_optimizer': False,
                'schedule_start_hour': 9,
                'schedule_end_hour': 17,
                'max_workers': 2,
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated['enable_optimizer'] is False
        assert updated['schedule_start_hour'] == 9
        assert updated['schedule_end_hour'] == 17
        assert updated['max_workers'] == 2

        final_response = client.get('/settings')
        assert final_response.status_code == 200
        final_payload = final_response.json()
        assert final_payload['enable_optimizer'] is False
        assert final_payload['schedule_start_hour'] == 9
        assert final_payload['schedule_end_hour'] == 17


def test_ui_basic_auth(monkeypatch):
    monkeypatch.setenv('OPTIMIZARR_UI_USERNAME', 'admin')
    monkeypatch.setenv('OPTIMIZARR_UI_PASSWORD', 'secret')

    with TestClient(app) as client:
        unauthorized = client.get('/settings')
        assert unauthorized.status_code == 401

        wrong = client.get('/settings', auth=('admin', 'wrong'))
        assert wrong.status_code == 401

        ok = client.get('/settings', auth=('admin', 'secret'))
        assert ok.status_code == 200


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


def test_scan_cancel_and_retry_endpoints(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    (media_root / 'nested').mkdir(parents=True)

    source_a = media_root / 'a.mkv'
    source_a.write_text('a')

    source_d = media_root / 'nested' / 'd.mkv'
    source_d.write_text('d')

    (media_root / 'b-1080p.mkv').write_text('already_optimized_name')

    source_c = media_root / 'c.mkv'
    source_c.write_text('c')
    (media_root / 'c-1080p.mkv').write_text('output_exists')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(routes, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        scan_response = client.post('/jobs/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']

        created_paths = {job['source_path'] for job in created_jobs}
        assert created_paths == {str(source_a), str(source_d)}

        target_job_id = created_jobs[0]['id']

        cancel_response = client.post(f'/jobs/{target_job_id}/cancel')
        assert cancel_response.status_code == 200
        cancelled = cancel_response.json()
        assert cancelled['status'] in {'cancelled', 'running'}

        retry_response = client.post(f'/jobs/{target_job_id}/retry')
        assert retry_response.status_code == 200
        retried = retry_response.json()
        assert retried['status'] in {'queued', 'cancelled', 'running'}


def test_scan_honors_process_hdr_only(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()

    hdr_file = media_root / 'hdr.mkv'
    hdr_file.write_text('hdr')
    sdr_file = media_root / 'sdr.mkv'
    sdr_file.write_text('sdr')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(routes, 'is_hdr_video', lambda path: path.endswith('hdr.mkv'))

    with TestClient(app) as client:
        client.post('/jobs/scan')

        # Enable HDR-only mode and ensure a new scan only queues HDR inputs.
        from app.core.database import SessionLocal
        from app.models.settings import Settings

        with SessionLocal() as session:
            settings = session.query(Settings).first()
            if settings is None:
                settings = Settings(process_hdr_only=True)
                session.add(settings)
            else:
                settings.process_hdr_only = True
            session.commit()

        scan_response = client.post('/jobs/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert {job['source_path'] for job in created_jobs} == {str(hdr_file)}
