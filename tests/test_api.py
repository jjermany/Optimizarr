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


def test_api_prefixed_health_endpoint():
    with TestClient(app) as client:
        response = client.get('/api/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


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
        assert isinstance(payload['requeue_interrupted_jobs'], bool)
        assert isinstance(payload['cleanup_workspaces_on_startup'], bool)
        assert payload['min_free_gb'] >= 1

        update_response = client.post(
            '/settings',
            json={
                'enable_optimizer': False,
                'max_workers': 2,
                'auto_discovery_enabled': False,
                'discovery_method': 'watcher',
                'discovery_interval_minutes': 15,
                'queue_sort': 'newest',
                'workspace_root': '/cache/workspaces',
                'requeue_interrupted_jobs': False,
                'cleanup_workspaces_on_startup': False,
                'min_free_gb': 32,
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
        assert updated['workspace_root'] == '/cache/workspaces'
        assert updated['requeue_interrupted_jobs'] is False
        assert updated['cleanup_workspaces_on_startup'] is False
        assert updated['min_free_gb'] == 32

        final_response = client.get('/settings')
        assert final_response.status_code == 200
        final_payload = final_response.json()
        assert final_payload['enable_optimizer'] is False


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
        assert updated['smtp_host'] == 'smtp.example.com'
        assert updated['smtp_port'] == 2525
        assert updated['to_emails'] == ['ops@example.com', 'alerts@example.com']
        assert updated['notify_on']['job_complete'] is True
        assert updated['notify_on']['job_failed'] is True
        assert updated['notify_on']['job_interrupted'] is True
        assert updated['notify_on']['low_disk_pause'] is True
        assert updated['notify_on']['recovery_ran'] is True

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
        assert profile_response.json()['hdr_only'] is True
        assert profile_response.json()['minimum_source_resolution'] == 2160
        assert profile_response.json()['schedule_policy'] == 'finish_current'
        assert profile_response.json()['output_conflict_policy'] == 'skip'

        update_profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={
                'hdr_only': True,
                'minimum_source_resolution': 3000,
                'codec': 'av1',
                'av1_fallback_codec': 'h264',
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


def test_delete_job_endpoint():
    with TestClient(app) as client:
        create_response = client.post('/jobs', json={'source_path': '/media/delete-me.mkv'})
        assert create_response.status_code == 201
        job_id = create_response.json()['id']

        delete_response = client.delete(f'/jobs/{job_id}')
        assert delete_response.status_code == 204

        get_response = client.get(f'/jobs/{job_id}')
        assert get_response.status_code == 404


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
                download_hash='abc123',
                client_type='qbittorrent',
                progress_percent=73,
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
    assert payload['download_hash'] is None
    assert payload['encode_job_id'] is None
    assert payload['completed_at'] is None

    with SessionLocal() as db:
        db_job = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
        assert db_job is not None
        assert db_job.status == DownloadJobStatus.pending.value
        assert db_job.completed_at is None
        assert db_job.error_message is None


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
        assert abort_response.json()['status'] == 'failed'
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
    from app.workers import queue as worker_queue

    monkeypatch.setattr(worker_queue, 'pause_queue', lambda reason='manual': paused_reasons.append(reason))
    monkeypatch.setattr(worker_queue, 'resume_queue', lambda reason='manual': (_ for _ in ()).throw(AssertionError('scan endpoints should not auto-resume queue')))
    monkeypatch.setattr(routes, 'scan_enabled_libraries', lambda db: [])
    monkeypatch.setattr(routes, 'scan_library', lambda db, library, include_disabled=False: [])

    with TestClient(app) as client:
        create_library = client.post('/libraries', json={'name': 'Scan Pause', 'path': str(library_path), 'enabled': True})
        assert create_library.status_code == 201
        library_id = create_library.json()['id']

        lib_scan = client.post(f'/libraries/{library_id}/scan')
        assert lib_scan.status_code == 200

        scan_all = client.post('/scan')
        assert scan_all.status_code == 200

    assert paused_reasons == ['manual_scan', 'manual_scan']


def test_clear_queue_endpoint_removes_encode_and_download_items(monkeypatch):
    from app.core.database import SessionLocal
    from app.models.download_job import DownloadJob, DownloadJobStatus
    from app.models.job import Job

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.commit()

        encode = Job(input_path='/media/clear-me.mkv', status='running', progress_percent=63, error_message='x')
        download = DownloadJob(
            source_file_path='/media/clear-me-download.mkv',
            status=DownloadJobStatus.searching.value,
            search_query='clear me',
            release_name='Release.Name',
            download_hash='deadbeef',
            client_type='qbittorrent',
            progress_percent=30,
        )
        db.add_all([encode, download])
        db.commit()
        db.refresh(encode)
        db.refresh(download)
        encode_id = encode.id
        download_id = download.id

    paused_reasons = []
    from app.workers import queue as worker_queue
    monkeypatch.setattr(worker_queue, 'pause_queue', lambda reason='manual': paused_reasons.append(reason))

    with TestClient(app) as client:
        response = client.post('/queue/clear')
        assert response.status_code == 200
        payload = response.json()
        assert payload['removed_job_ids'] == [encode_id]
        assert payload['removed_download_job_ids'] == [download_id]

    with SessionLocal() as db:
        updated_encode = db.query(Job).filter(Job.id == encode_id).first()
        updated_download = db.query(DownloadJob).filter(DownloadJob.id == download_id).first()
        assert updated_encode is None
        assert updated_download is None

    assert paused_reasons == ['manual']


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
                assert job.status == 'failed'
                assert job.error_message == 'Aborted by user'
                assert job.progress_percent == 0
                assert job.fps is None
                assert job.eta_seconds is None
                assert job.output_path is None




def test_remove_all_jobs_endpoint_removes_only_terminal_jobs():
    from app.core.database import SessionLocal
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
            db.commit()

        response = client.post('/jobs/remove-all')
        assert response.status_code == 200
        payload = response.json()
        assert complete_id in payload['removed_job_ids']
        assert queued_id not in payload['removed_job_ids']

        with SessionLocal() as db:
            assert db.query(Job).filter(Job.id == complete_id).first() is None
            assert db.query(Job).filter(Job.id == queued_id).first() is not None

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


def test_cleanup_optimized_endpoint_deletes_recorded_outputs(tmp_path):
    from app.core.database import SessionLocal
    from app.models.job import Job

    output_file = tmp_path / 'movie-opt.mkv'
    output_file.write_text('optimized')

    with TestClient(app) as client:
        created = client.post('/jobs', json={'source_path': '/media/source-hdr.mkv'})
        assert created.status_code == 201
        job_id = created.json()['id']

        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert job is not None
            job.status = 'complete'
            job.output_path = str(output_file)
            db.commit()

        cleanup_response = client.post('/cleanup/optimized')
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload['deleted_files'] >= 1
        assert job_id in payload['affected_job_ids']

    assert not output_file.exists()
