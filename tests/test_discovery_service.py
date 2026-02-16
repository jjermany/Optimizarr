from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services import discovery_service


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

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_file)
