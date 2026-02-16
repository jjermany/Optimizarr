import time

from fastapi.testclient import TestClient

from app.api import routes
from app.services import discovery_service
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
        assert 'global_quiet_enabled' in payload
        assert 'auto_discovery_enabled' in payload
        assert payload['discovery_method'] in {'interval', 'watcher'}
        assert payload['discovery_interval_minutes'] >= 1
        assert payload['workspace_root']

        update_response = client.post(
            '/settings',
            json={
                'enable_optimizer': False,
                'schedule_start_hour': 9,
                'schedule_end_hour': 17,
                'max_workers': 2,
                'global_quiet_enabled': True,
                'global_quiet_start_hour': 23,
                'global_quiet_end_hour': 5,
                'auto_discovery_enabled': False,
                'discovery_method': 'watcher',
                'discovery_interval_minutes': 15,
                'workspace_root': '/cache/workspaces',
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated['enable_optimizer'] is False
        assert updated['schedule_start_hour'] == 9
        assert updated['schedule_end_hour'] == 17
        assert updated['max_workers'] == 2
        assert updated['global_quiet_enabled'] is True
        assert updated['global_quiet_start_hour'] == 23
        assert updated['global_quiet_end_hour'] == 5
        assert updated['auto_discovery_enabled'] is False
        assert updated['discovery_method'] == 'watcher'
        assert updated['discovery_interval_minutes'] == 15
        assert updated['workspace_root'] == '/cache/workspaces'

        final_response = client.get('/settings')
        assert final_response.status_code == 200
        final_payload = final_response.json()
        assert final_payload['enable_optimizer'] is False
        assert final_payload['schedule_start_hour'] == 9
        assert final_payload['schedule_end_hour'] == 17


def test_get_and_update_notification_settings_and_test_endpoint(monkeypatch):
    queued = []
    monkeypatch.setattr(routes.notification_service, 'enqueue_test_email', lambda: queued.append('sent'))

    with TestClient(app) as client:
        get_response = client.get('/notifications/settings')
        assert get_response.status_code == 200
        payload = get_response.json()
        assert 'smtp_host' in payload
        assert 'notify_on' in payload

        update_response = client.put(
            '/notifications/settings',
            json={
                'smtp_host': 'smtp.example.com',
                'smtp_port': 2525,
                'smtp_user': 'user',
                'smtp_password': 'pass',
                'smtp_tls': True,
                'from_email': 'optimizarr@example.com',
                'to_emails': ['ops@example.com', 'alerts@example.com'],
                'notify_on': {'job_failed': True, 'job_complete': False, 'batch_complete': True},
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated['smtp_host'] == 'smtp.example.com'
        assert updated['smtp_port'] == 2525
        assert updated['to_emails'] == ['ops@example.com', 'alerts@example.com']
        assert updated['notify_on']['job_failed'] is True

        test_response = client.post('/notifications/test')
        assert test_response.status_code == 202
        assert test_response.json() == {'status': 'queued'}

    assert queued == ['sent']


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

        update_profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={
                'hdr_only': True,
                'codec': 'av1',
                'av1_fallback_codec': 'h264',
                'max_workers': 2,
                'schedule_start_hour': 3,
                'schedule_end_hour': 11,
            },
        )
        assert update_profile_response.status_code == 200
        updated_profile = update_profile_response.json()
        assert updated_profile['hdr_only'] is True
        assert updated_profile['codec'] == 'av1'
        assert updated_profile['av1_fallback_codec'] == 'h264'
        assert updated_profile['schedule_start_hour'] == 3
        assert updated_profile['schedule_end_hour'] == 11

        invalid_path_response = client.post('/libraries', json={'name': 'Bad', 'path': '/tmp/not-allowed'})
        assert invalid_path_response.status_code == 422

        update_library_response = client.put(f'/libraries/{library_id}', json={'enabled': False})
        assert update_library_response.status_code == 200
        assert update_library_response.json()['enabled'] is False

        delete_response = client.delete(f'/libraries/{library_id}')
        assert delete_response.status_code == 204


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

        scan_response = client.post('/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_enabled)

        from app.core.database import SessionLocal
        from app.models.job import Job

        with SessionLocal() as session:
            job = session.query(Job).filter(Job.id == created_jobs[0]['id']).first()
            assert job is not None
            assert job.library_id == enabled_library_id
            assert job.profile_snapshot_json is not None



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

        profile_update = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': True})
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
            assert job.profile_snapshot_json == first_snapshot
