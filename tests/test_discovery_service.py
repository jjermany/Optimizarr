from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services import discovery_service
from app.services.realtime_service import broker


def test_scan_library_respects_hdr_only_profile(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'movies'
    library_path.mkdir()

    source_file = library_path / 'movie.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Movies', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': True})
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []


def test_scan_library_hdr_only_ignores_resolution_filters(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'hdr'
    library_path.mkdir()

    source_file = library_path / 'movie-hdr.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 1080)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'HDR', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'target_resolution': 1080, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_file)


def test_scan_library_queues_4k_when_hdr_not_required(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'shows'
    library_path.mkdir()

    source_file = library_path / 'episode.mp4'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Shows', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': False})
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_file)


def test_scan_library_respects_minimum_source_and_target_resolution(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'custom'
    library_path.mkdir()

    larger_file = library_path / 'big.mkv'
    larger_file.write_text('big')
    equal_file = library_path / 'equal.mkv'
    equal_file.write_text('equal')

    def fake_probe(path: str) -> int:
        return 1400 if path.endswith('big.mkv') else 1337

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', fake_probe)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Custom', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'target_resolution': 1337, 'minimum_source_resolution': 1400},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(larger_file)


def test_scan_library_publishes_discovery_job_queued_event(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'docs'
    library_path.mkdir()

    source_file = library_path / 'clip.mkv'
    source_file.write_text('content')

    events = []
    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)
    monkeypatch.setattr(broker, 'publish_system_event', lambda event, **data: events.append((event, data)))

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Docs', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': False})
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200

    discovery_events = [item for item in events if item[0] == 'discovery_job_queued']
    assert len(discovery_events) == 1
    assert discovery_events[0][1]['source_path'] == str(source_file)


def test_scan_library_skips_disabled_library_by_default(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'disabled'
    library_path.mkdir()

    source_file = library_path / 'movie.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Disabled', 'path': str(library_path), 'enabled': False})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        from app.core.database import SessionLocal
        from app.models.library import Library

        with SessionLocal() as session:
            library = session.query(Library).filter(Library.id == library_id).first()
            assert library is not None
            created_jobs = discovery_service.scan_library(session, library)
            assert created_jobs == []


def test_scan_library_can_requeue_source_after_abort_all(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'movies'
    library_path.mkdir()

    source_file = library_path / 'movie.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Movies', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': False})
        assert profile_response.status_code == 200

        first_scan_response = client.post(f'/libraries/{library_id}/scan')
        assert first_scan_response.status_code == 200
        first_jobs = first_scan_response.json()['created_jobs']
        assert len(first_jobs) == 1

        abort_all_response = client.post('/jobs/abort-all')
        assert abort_all_response.status_code == 200
        assert first_jobs[0]['id'] in abort_all_response.json()['aborted_job_ids']

        second_scan_response = client.post(f'/libraries/{library_id}/scan')
        assert second_scan_response.status_code == 200
        second_jobs = second_scan_response.json()['created_jobs']
        assert len(second_jobs) == 1
        assert second_jobs[0]['id'] != first_jobs[0]['id']
        assert second_jobs[0]['source_path'] == str(source_file)
