from fastapi.testclient import TestClient

import pytest

from app.api import routes
from app.core.database import SessionLocal
from app.main import app
from app.models.discovery_index import DiscoveryFileIndex
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.services import discovery_service
from app.services import download_monitor_service
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


def test_scan_library_hdr_only_respects_minimum_source_resolution(monkeypatch, tmp_path):
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
        assert created_jobs == []


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


def test_scan_library_skips_hdr_probe_when_profile_does_not_need_it(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'shows'
    library_path.mkdir()

    source_file = library_path / 'episode.mp4'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)

    hdr_calls = {'count': 0}

    def fake_hdr(_path: str) -> bool:
        hdr_calls['count'] += 1
        return False

    monkeypatch.setattr(discovery_service, 'is_hdr_video', fake_hdr)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Shows', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': False, 'tone_map_hdr': False})
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert len(scan_response.json()['created_jobs']) == 1

    assert hdr_calls['count'] == 0


def test_scan_library_tone_map_hdr_requires_hdr_source(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'tone-map-hdr-required'
    library_path.mkdir()

    source_file = library_path / 'movie.2160p.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2160)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'ToneMapRequired', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'tone_map_hdr': True, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []


def test_scan_library_checks_hdr_before_resolution_for_hdr_required_profiles(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'tone-map-hdr-required'
    library_path.mkdir()

    source_file = library_path / 'movie.2160p.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)

    resolution_calls = {'count': 0}

    def fake_probe(_path: str) -> int:
        resolution_calls['count'] += 1
        return 2160

    monkeypatch.setattr(discovery_service, 'probe_video_height', fake_probe)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'ToneMapRequired', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'tone_map_hdr': True, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []

    assert resolution_calls['count'] == 0


def test_scan_library_tone_map_hdr_queues_hdr_source(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'tone-map-hdr-queue'
    library_path.mkdir()

    source_file = library_path / 'movie.2160p.HDR10.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2080)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'ToneMapHdrQueue', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': False, 'tone_map_hdr': True, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        created_jobs = scan_response.json()['created_jobs']
        assert len(created_jobs) == 1
        assert created_jobs[0]['source_path'] == str(source_file)


def test_scan_library_hdr_only_accepts_near_4k_when_labeled_2160(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'hdr-near-4k'
    library_path.mkdir()

    source_file = library_path / 'Movie.Title.2024.2160p.HDR10-GROUP.mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _: 2080)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: True)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Near4K', 'path': str(library_path), 'enabled': True})
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


def test_scan_library_hdr_only_skips_when_target_sdr_sibling_exists(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'hdr-with-sdr'
    library_path.mkdir()

    hdr_file = library_path / 'Movie.Title.2024.2160p.HDR10-GROUP.mkv'
    hdr_file.write_text('hdr')
    sdr_target = library_path / 'Movie.Title.2024.1080p.BluRay-GROUP.mkv'
    sdr_target.write_text('sdr')

    def fake_probe(path: str) -> int:
        if path.endswith('2160p.HDR10-GROUP.mkv'):
            return 2080
        if path.endswith('1080p.BluRay-GROUP.mkv'):
            return 1080
        return 0

    def fake_hdr(path: str) -> bool:
        return path.endswith('2160p.HDR10-GROUP.mkv')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', fake_probe)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', fake_hdr)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'HdrWithSdr', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'target_resolution': 1080, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []


def test_scan_library_hdr_only_treats_2k_sibling_as_existing_1080_target(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'hdr-with-2k-sdr'
    library_path.mkdir()

    hdr_file = library_path / 'Movie.Title.2024.2160p.HDR10-GROUP.mkv'
    hdr_file.write_text('hdr')
    sdr_target = library_path / 'Movie.Title.2024.2K.BluRay-GROUP.mkv'
    sdr_target.write_text('sdr')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda path: 2080 if '2160p' in path else 1080)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda path: 'HDR10' in path)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'HdrWith2kSdr', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'hdr_only': True, 'target_resolution': 1080, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []


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


def test_run_watcher_recovery_only_processes_new_or_changed_files(monkeypatch, tmp_path):
    library_path = tmp_path / 'movies'
    library_path.mkdir()

    unchanged_file = library_path / 'Existing Movie (2024).mkv'
    unchanged_file.write_text('existing')
    new_file = library_path / 'New Movie (2025).mkv'
    new_file.write_text('new')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(DiscoveryFileIndex).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=False, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        db.add(
            DiscoveryFileIndex(
                library_id=library.id,
                source_path=str(unchanged_file),
                file_size_bytes=unchanged_file.stat().st_size,
                file_mtime_ns=unchanged_file.stat().st_mtime_ns,
                discovery_signature=discovery_service._discovery_signature(profile),
            )
        )
        db.commit()

        probe_calls = []
        monkeypatch.setattr(
            discovery_service,
            'probe_video_height',
            lambda path: probe_calls.append(path) or 2160,
        )
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        summary = discovery_service.run_watcher_recovery(db, [library], reason='startup')
        queued_jobs = db.query(Job).order_by(Job.id.asc()).all()

        assert summary['changed_files'] == 1
        assert summary['queued_jobs'] == 1
        assert [job.source_path for job in queued_jobs] == [str(new_file)]
        assert probe_calls == [str(new_file)]


def test_run_watcher_recovery_reprocesses_indexed_file_when_profile_signature_changes(monkeypatch, tmp_path):
    library_path = tmp_path / 'movies'
    library_path.mkdir()

    source_file = library_path / 'Signature Test (2025).mkv'
    source_file.write_text('content')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(DiscoveryFileIndex).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=False, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        db.add(
            DiscoveryFileIndex(
                library_id=library.id,
                source_path=str(source_file),
                file_size_bytes=source_file.stat().st_size,
                file_mtime_ns=source_file.stat().st_mtime_ns,
                discovery_signature='stale-signature',
            )
        )
        db.commit()

        probe_calls = []
        monkeypatch.setattr(
            discovery_service,
            'probe_video_height',
            lambda path: probe_calls.append(path) or 2160,
        )
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        summary = discovery_service.run_watcher_recovery(db, [library], reason='startup')

        assert summary['changed_files'] == 1
        assert summary['queued_jobs'] == 1
        assert probe_calls == [str(source_file)]


def test_watcher_recovery_skips_radarr_upgrade_when_optimized_copy_exists(monkeypatch, tmp_path):
    library_path = tmp_path / 'movies'
    library_path.mkdir()

    upgraded_source = library_path / 'Example Movie (2026) Remux 2160p.mkv'
    existing_optimized = library_path / 'Example Movie (2026)-1080p.mkv'
    upgraded_source.write_text('new 4k source')
    existing_optimized.write_text('existing optimized')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(DiscoveryFileIndex).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(
            library_id=library.id,
            download_enabled=False,
            hdr_only=False,
            target_resolution=1080,
            output_suffix='-1080p',
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

        monkeypatch.setattr(
            discovery_service,
            'probe_video_height',
            lambda path: pytest.fail(f'watcher should not probe already optimized identity: {path}'),
        )

        summary = discovery_service.run_watcher_recovery(db, [library], reason='watcher')
        queued_jobs = db.query(Job).all()

        assert summary['changed_files'] == 2
        assert summary['queued_jobs'] == 0
        assert queued_jobs == []


def test_queue_file_if_eligible_skips_radarr_upgrade_when_completed_optimized_job_exists(monkeypatch, tmp_path):
    library_path = tmp_path / 'movies'
    library_path.mkdir()

    old_source = library_path / 'Example Movie (2026) WebDL 2160p.mkv'
    upgraded_source = library_path / 'Example Movie (2026) Remux 2160p.mkv'
    existing_optimized = library_path / 'Example Movie (2026)-1080p.mkv'
    old_source.write_text('old source placeholder')
    upgraded_source.write_text('new 4k source')
    existing_optimized.write_text('existing optimized')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(
            library_id=library.id,
            download_enabled=False,
            hdr_only=False,
            target_resolution=1080,
            output_suffix='-1080p',
        )
        db.add(profile)
        db.add(Job(
            input_path=str(old_source),
            output_path=str(existing_optimized),
            status='complete',
            library_id=library.id,
        ))
        db.commit()
        db.refresh(profile)

        monkeypatch.setattr(
            discovery_service,
            'probe_video_height',
            lambda path: pytest.fail(f'watcher should not probe completed optimized identity: {path}'),
        )

        job = discovery_service._queue_file_if_eligible(db, upgraded_source, library, profile)

        assert job is None


def test_scan_library_tracks_active_scan_state(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'shows'
    library_path.mkdir()

    source_file = library_path / 'episode.mkv'
    source_file.write_text('content')

    events = []
    observed_scan_state = []

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _: False)
    monkeypatch.setattr(broker, 'publish_system_event', lambda event, **data: events.append((event, data)))

    def fake_probe(path: str) -> int:
        observed_scan_state.append(discovery_service.is_library_scan_active(library_id))
        return 2160

    monkeypatch.setattr(discovery_service, 'probe_video_height', fake_probe)

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Shows', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(f'/libraries/{library_id}/profile', json={'hdr_only': False})
        assert profile_response.status_code == 200

        assert discovery_service.is_library_scan_active(library_id) is False
        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert discovery_service.is_library_scan_active(library_id) is False

    assert observed_scan_state == [True]
    assert [event for event, _data in events if event.startswith('library_scan_')] == [
        'library_scan_started',
        'library_scan_completed',
    ]
    progress_events = [data for event, data in events if event == 'manual_library_scan_progress']
    assert progress_events
    assert progress_events[0]['progress_percent'] == 1
    assert progress_events[-1]['progress_percent'] == 100
    assert all(
        earlier['progress_percent'] < later['progress_percent']
        for earlier, later in zip(progress_events, progress_events[1:])
    )
    assert all(event['library_id'] == library_id for event in progress_events)


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


def test_queue_file_if_eligible_download_mode_does_not_queue_when_probe_fails(monkeypatch, tmp_path):
    media_file = tmp_path / 'Doctor Strange (2016).mkv'
    media_file.write_text('content')

    with SessionLocal() as db:
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(tmp_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        created = []
        monkeypatch.setattr(download_monitor_service, 'recover_completed_artifact_for_source', lambda *_args, **_kwargs: False)
        monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
        monkeypatch.setattr(download_monitor_service, 'download_job_exists_for_source', lambda _db, _path: False)
        monkeypatch.setattr(download_monitor_service, 'create_download_job', lambda _db, source_path, *_args: created.append(source_path))

        # Probe failure should still block queueing in download-enabled mode.
        monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: None)
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        result = discovery_service._queue_file_if_eligible(db, media_file, library, profile)
        assert result is None
        assert created == []


def test_download_mode_still_respects_resolution_and_hdr_filters(monkeypatch, tmp_path):
    media_root = tmp_path / 'media'
    media_root.mkdir()
    library_path = media_root / 'movies'
    library_path.mkdir()

    source_file = library_path / 'Doctor Strange (2016).mkv'
    source_file.write_text('content')

    monkeypatch.setattr(routes, 'MEDIA_ROOT', media_root)
    monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: 1080)
    monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)
    monkeypatch.setattr(download_monitor_service, 'recover_completed_artifact_for_source', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)

    created_downloads = []
    monkeypatch.setattr(
        download_monitor_service,
        'create_download_job',
        lambda _db, source_path, *_args: created_downloads.append(source_path),
    )

    with TestClient(app) as client:
        create_response = client.post('/libraries', json={'name': 'Movies', 'path': str(library_path), 'enabled': True})
        assert create_response.status_code == 201
        library_id = create_response.json()['id']

        profile_response = client.put(
            f'/libraries/{library_id}/profile',
            json={'download_enabled': True, 'hdr_only': True, 'minimum_source_resolution': 2160},
        )
        assert profile_response.status_code == 200

        scan_response = client.post(f'/libraries/{library_id}/scan')
        assert scan_response.status_code == 200
        assert scan_response.json()['created_jobs'] == []

    assert created_downloads == []


def test_queue_file_if_eligible_download_mode_creates_download_job_when_route_ready(monkeypatch, tmp_path):
    media_file = tmp_path / 'Kraven the Hunter (2024).mkv'
    media_file.write_text('content')

    with SessionLocal() as db:
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(tmp_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        created_downloads = []
        monkeypatch.setattr(download_monitor_service, 'recover_completed_artifact_for_source', lambda *_args, **_kwargs: False)
        monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
        monkeypatch.setattr(download_monitor_service, 'download_job_exists_for_source', lambda _db, _path: False)
        monkeypatch.setattr(
            download_monitor_service,
            'create_download_job',
            lambda _db, source_path, *_args: created_downloads.append(source_path),
        )
        monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: 2160)
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        result = discovery_service._queue_file_if_eligible(db, media_file, library, profile)
        assert result is None
        assert created_downloads == [str(media_file)]


def test_queue_file_if_eligible_download_mode_imports_completed_artifact_before_routing(monkeypatch, tmp_path):
    media_file = tmp_path / 'Shrek 2 (2004).mkv'
    media_file.write_text('content')

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(tmp_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        created_downloads = []
        cancelled_sources = []
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            download_monitor_service,
            'recover_completed_artifact_for_source',
            lambda _db, source_path, *_args, **_kwargs: source_path == str(media_file),
        )
        monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
        monkeypatch.setattr(download_monitor_service, 'download_job_exists_for_source', lambda _db, _path: False)
        monkeypatch.setattr(
            download_monitor_service,
            'create_download_job',
            lambda _db, source_path, *_args: created_downloads.append(source_path),
        )
        monkeypatch.setattr(
            discovery_service,
            '_cancel_queued_encode_for_source',
            lambda _db, source_path, library_id: cancelled_sources.append((source_path, library_id)),
        )
        monkeypatch.setattr(
            discovery_service.broker,
            'publish_system_event',
            lambda event, **payload: events.append((event, payload)),
        )
        monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: 2160)
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        result = discovery_service._queue_file_if_eligible(db, media_file, library, profile)

        assert result is None
        assert created_downloads == []
        assert cancelled_sources == [(str(media_file), library.id)]
        assert ('discovery_download_imported', {'source_path': str(media_file), 'library_id': library.id}) in events


def test_queue_file_if_eligible_download_mode_skips_when_source_already_moving(monkeypatch, tmp_path):
    media_file = tmp_path / 'Already Moving (2025).mkv'
    media_file.write_text('content')

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(tmp_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        moving = DownloadJob(
            library_id=library.id,
            source_file_path=str(media_file),
            status=DownloadJobStatus.moving.value,
            client_type='sabnzbd',
            progress_percent=99,
        )
        db.add(moving)
        db.commit()

        create_calls = []
        monkeypatch.setattr(download_monitor_service, 'recover_completed_artifact_for_source', lambda *_args, **_kwargs: False)
        monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
        monkeypatch.setattr(
            download_monitor_service,
            'create_download_job',
            lambda _db, source_path, *_args: create_calls.append(source_path),
        )
        monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: 2160)
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        result = discovery_service._queue_file_if_eligible(db, media_file, library, profile)

        assert result is None
        assert create_calls == []


def test_queue_file_if_eligible_download_mode_removes_stale_placeholder_encode(monkeypatch, tmp_path):
    media_file = tmp_path / 'The Unholy Trinity (2025).mkv'
    media_file.write_text('content')

    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path=str(tmp_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, download_enabled=True, hdr_only=False, target_resolution=1080)
        db.add(profile)
        db.commit()
        db.refresh(profile)

        placeholder = Job(
            input_path=str(media_file),
            output_path='',
            status='queued',
            library_id=library.id,
        )
        db.add(placeholder)
        db.commit()
        db.refresh(placeholder)

        created_downloads = []
        monkeypatch.setattr(download_monitor_service, 'recover_completed_artifact_for_source', lambda *_args, **_kwargs: False)
        monkeypatch.setattr(download_monitor_service, 'can_attempt_download', lambda _db: True)
        monkeypatch.setattr(download_monitor_service, 'download_job_exists_for_source', lambda _db, _path: False)
        monkeypatch.setattr(
            download_monitor_service,
            'create_download_job',
            lambda _db, source_path, *_args: created_downloads.append(source_path),
        )
        monkeypatch.setattr(discovery_service, 'probe_video_height', lambda _path: 2160)
        monkeypatch.setattr(discovery_service, 'is_hdr_video', lambda _path: False)

        result = discovery_service._queue_file_if_eligible(db, media_file, library, profile)
        placeholder_after = db.query(Job).filter(Job.id == placeholder.id).first()

        assert result is None
        assert created_downloads == [str(media_file)]
        assert placeholder_after is None
