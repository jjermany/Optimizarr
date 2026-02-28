from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.library import DownloadQualityProfileEnum
from app.services.download_monitor_service import (
    _build_search_query,
    _check_download_progress,
    _process_searching_jobs,
    _select_best_release,
    download_job_exists_for_source,
    run_download_startup_recovery,
    run_scan_recovery,
)



def _profile(quality: DownloadQualityProfileEnum):
    return SimpleNamespace(target_resolution=1080, download_quality_profile=quality.value)



def test_select_best_release_web_dl_rejects_webrip_only_titles():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-GROUP',
            'seeders': 999,
            'size': 1000,
            'protocol': 'torrent',
        }
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is None



def test_select_best_release_mixed_tags_resolve_deterministically_for_web_dl():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.WEB-DL.x265-GROUP',
            'seeders': 10,
            'size': 1500,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-OTHER',
            'seeders': 500,
            'size': 1200,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEBRip.WEB-DL.x265-GROUP'



def test_select_best_release_any_allows_all_quality_classes():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEBRip.x265-HIGHSEED',
            'seeders': 500,
            'size': 1400,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.BluRay.x265-LOWSEED',
            'seeders': 50,
            'size': 1100,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.any), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEBRip.x265-HIGHSEED'


def test_select_best_release_uses_structured_quality_when_title_is_ambiguous():
    releases = [
        {
            'title': 'Movie 2024 1080p Proper',
            'quality': {'name': 'WEB-DL 1080p'},
            'seeders': 25,
            'size': 1500,
            'protocol': 'torrent',
        }
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie 2024 1080p Proper'


def test_select_best_release_accepts_1920x1080_resolution_format():
    releases = [
        {
            'title': 'Movie.2024.1920x1080.WEB-DL.x265-GROUP',
            'seeders': 40,
            'size': 1800,
            'protocol': 'torrent',
        }
    ]

    selected = _select_best_release(releases, _profile(DownloadQualityProfileEnum.web_dl), qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1920x1080.WEB-DL.x265-GROUP'


def test_download_job_exists_for_source_treats_pending_as_active_non_terminal():
    source_path = '/media/download-pending.mkv'
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.commit()

        pending_job = DownloadJob(source_file_path=source_path, status=DownloadJobStatus.pending.value)
        failed_job = DownloadJob(source_file_path='/media/download-failed.mkv', status=DownloadJobStatus.failed.value)
        db.add_all([pending_job, failed_job])
        db.commit()

        assert download_job_exists_for_source(db, source_path) is True
        assert download_job_exists_for_source(db, '/media/download-failed.mkv') is False


from app.models.library import Library, LibraryProfile
from app.services import download_client_service


def _seed_library_with_profile(db):
    library = Library(name='Movies', path='/tmp/movies', enabled=True)
    db.add(library)
    db.commit()
    db.refresh(library)

    profile = LibraryProfile(library_id=library.id, download_enabled=True, tone_map_hdr=False)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return library


def test_startup_recovery_imports_match_from_completed_download_root(monkeypatch, tmp_path):
    completed_root = tmp_path / 'complete'
    candidate = completed_root / 'The.Gorge.2025.1080p.WEB-DL'
    candidate.mkdir(parents=True)
    media_file = candidate / 'The.Gorge.2025.1080p.WEB-DL.mkv'
    media_file.write_bytes(b'x')

    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/The Gorge (2025).mkv',
            release_name='The.Gorge.2025.1080p.WEB-DL',
            download_hash='deadbeef',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: str(completed_root))

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)

        assert summary['imported'] == 1
        assert summary['reset_to_searching'] == 0
        assert imported_paths == [str(candidate)]


def test_check_download_progress_recovers_stale_qbit_hash_and_imports(monkeypatch):
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/The Gorge (2025).mkv',
            release_name='The.Gorge.2025.1080p.WEB-DL',
            download_hash='stalehash',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=False)

        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 0,
            'is_complete': False,
            'is_stalled': False,
            'save_path': None,
        })
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'The.Gorge.2025.1080p.WEB-DL',
            'hash': 'newhash',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/The.Gorge.2025.1080p.WEB-DL',
            'added_on': 1,
        }])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.download_hash == 'newhash'
        assert dj.progress_percent == 100
        assert imported_paths == ['/downloads/The.Gorge.2025.1080p.WEB-DL']


# ─────────────────────────────────────────────────────────────────────────────
# Search query construction tests
# ─────────────────────────────────────────────────────────────────────────────

def _full_profile(quality: DownloadQualityProfileEnum):
    """A profile stub with all fields _build_search_query reads."""
    return SimpleNamespace(target_resolution=1080, download_quality_profile=quality.value)


def test_build_search_query_does_not_include_quality_keyword():
    """Quality term must NOT appear in the Prowlarr search — filtering is done client-side."""
    profile = _full_profile(DownloadQualityProfileEnum.web_dl)
    query = _build_search_query('/media/The Gorge (2025).mkv', profile)
    assert 'WEB-DL' not in query
    assert 'WEB' not in query
    assert 'webdl' not in query.lower()
    assert 'The Gorge' in query
    assert '2025' in query
    assert '1080p' in query


def test_build_search_query_excludes_quality_for_all_profiles():
    """Quality term is excluded regardless of the configured profile."""
    for quality in DownloadQualityProfileEnum:
        profile = _full_profile(quality)
        query = _build_search_query('/media/Inception (2010).mkv', profile)
        assert 'remux' not in query.lower()
        assert 'web-dl' not in query.lower()
        assert 'webrip' not in query.lower()
        assert 'bluray' not in query.lower()
        assert 'hdtv' not in query.lower()
        assert 'Inception' in query
        assert '2010' in query


# ─────────────────────────────────────────────────────────────────────────────
# Timeout ordering: completion must be checked before timeout fires
# ─────────────────────────────────────────────────────────────────────────────

def test_check_download_progress_imports_complete_download_despite_elapsed_timeout(monkeypatch):
    """A download that is complete in qBit must be imported even when the timeout has elapsed."""
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        # Use a very short 1-minute timeout with a start time that is already past it.
        profile = db.query(LibraryProfile).filter_by(library_id=library.id).first()
        profile.download_timeout_minutes = 1
        db.commit()

        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Inception (2010).mkv',
            release_name='Inception.2010.1080p.WEB-DL',
            download_hash='aabbccdd',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
            # download started 5 minutes ago — well past the 1-minute timeout
            download_started_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=False)

        # qBit reports the torrent as complete
        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 100,
            'is_complete': True,
            'is_stalled': False,
            'save_path': '/downloads/Inception.2010.1080p.WEB-DL',
        })
        monkeypatch.setattr(download_client_service, 'tag_qbt_torrent', lambda *_args, **_kw: True)

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        # The download was complete so it must be imported, not timed out
        assert dj.status == DownloadJobStatus.complete.value
        assert imported_paths == ['/downloads/Inception.2010.1080p.WEB-DL']


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery: timed_out jobs that completed in qBit while offline
# ─────────────────────────────────────────────────────────────────────────────

def test_startup_recovery_imports_timed_out_job_completed_in_qbit(monkeypatch):
    """Jobs in timed_out status that have a completed torrent in qBit are imported on startup."""
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Dune (2021).mkv',
            release_name='Dune.2021.1080p.WEB-DL',
            download_hash='deadbeef',
            client_type='qbittorrent',
            status=DownloadJobStatus.timed_out.value,
            error_message='Download timed out after 60 minutes',
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'Dune.2021.1080p.WEB-DL',
            'hash': 'deadbeef',
            'state': 'uploading',  # completed state
            'progress': 1.0,
            'content_path': '/downloads/complete/Dune.2021.1080p.WEB-DL',
            'save_path': '/downloads/complete/Dune.2021.1080p.WEB-DL',
            'added_on': 1,
        }])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _dj.error_message = None
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 1
        assert dj.status == DownloadJobStatus.complete.value
        assert dj.error_message is None
        assert imported_paths == ['/downloads/complete/Dune.2021.1080p.WEB-DL']


def test_startup_recovery_skips_import_when_completed_release_does_not_match_profile(monkeypatch):
    """Completed torrents that do not match profile filters must not be imported."""
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Example (2024).mkv',
            release_name='Example.2024.720p.WEB-DL',
            download_hash='badmatch',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'Example.2024.720p.WEB-DL',
            'hash': 'badmatch',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/complete/Example.2024.720p.WEB-DL',
            'save_path': '/downloads/complete/Example.2024.720p.WEB-DL',
            'added_on': 1,
        }])

        import_calls = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._import_file',
            lambda *_args, **_kwargs: import_calls.append(True),
        )

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 0
        assert summary['reset_to_searching'] == 1
        assert dj.status == DownloadJobStatus.searching.value
        assert dj.download_hash is None
        assert import_calls == []


def test_startup_recovery_skips_timed_out_job_not_in_qbit(monkeypatch):
    """timed_out jobs with no matching torrent in qBit are left unchanged."""
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Dune (2021).mkv',
            release_name='Dune.2021.1080p.WEB-DL',
            download_hash='badhash',
            client_type='qbittorrent',
            status=DownloadJobStatus.timed_out.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 0
        # Status must remain timed_out — no torrent found to recover
        assert dj.status == DownloadJobStatus.timed_out.value


def test_startup_recovery_imports_failed_job_completed_in_qbit(monkeypatch):
    """Jobs in ANY non-importing status with a completed qBit torrent are imported at startup."""
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        # Job is in 'failed' status (search returned no results previously)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Oppenheimer (2023).mkv',
            release_name='Oppenheimer.2023.1080p.WEB-DL',
            download_hash='cafebabe',
            client_type='qbittorrent',
            status=DownloadJobStatus.failed.value,
            error_message='No matching release found',
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'Oppenheimer.2023.1080p.WEB-DL',
            'hash': 'cafebabe',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/complete/Oppenheimer.2023.1080p.WEB-DL',
            'save_path': '/downloads/complete/Oppenheimer.2023.1080p.WEB-DL',
            'added_on': 1,
        }])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _dj.error_message = None
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 1
        assert dj.status == DownloadJobStatus.complete.value
        assert imported_paths == ['/downloads/complete/Oppenheimer.2023.1080p.WEB-DL']


# ─────────────────────────────────────────────────────────────────────────────
# Prowlarr search error: job stays in 'searching' state for retry
# ─────────────────────────────────────────────────────────────────────────────

def test_check_search_job_stays_searching_on_prowlarr_error(monkeypatch):
    """When Prowlarr returns None (connection error) the job stays in 'searching' for retry."""
    from app.services import prowlarr_service
    from app.services.download_monitor_service import _do_search

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Interstellar (2014).mkv',
            status=DownloadJobStatus.searching.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        prowlarr_stub = SimpleNamespace(enabled=True, host='http://prowlarr', api_key='key')
        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=False)

        # Simulate a connection failure — search returns None
        monkeypatch.setattr(prowlarr_service, 'search', lambda *_args, **_kw: None)

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        # Job must remain in 'searching' so the monitor retries on the next cycle
        assert dj.status == DownloadJobStatus.searching.value


def test_process_searching_jobs_skips_work_when_main_queue_is_paused(monkeypatch):
    """Download search worker must not run while the main queue is paused."""
    from app.services import download_client_service, prowlarr_service
    from app.workers import queue as worker_queue

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.commit()

        dj = DownloadJob(
            library_id=None,
            source_file_path='/media/Paused.Queue.Test.mkv',
            status=DownloadJobStatus.pending.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(worker_queue, 'is_queue_paused', lambda: True)
        monkeypatch.setattr(prowlarr_service, 'get_or_create_prowlarr_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        search_calls = []
        monkeypatch.setattr('app.services.download_monitor_service._do_search', lambda *_args, **_kwargs: search_calls.append(True))

        _process_searching_jobs(db)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.pending.value
        assert search_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# run_scan_recovery: mid-runtime qBit reconciliation after a library scan
# ─────────────────────────────────────────────────────────────────────────────

def test_scan_recovery_imports_stalled_job_completed_in_qbit(monkeypatch):
    """run_scan_recovery imports a stalled job whose torrent finished in qBit."""
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Oppenheimer (2023).mkv',
            release_name='Oppenheimer.2023.1080p.WEB-DL',
            download_hash='aabbccdd',
            client_type='qbittorrent',
            status=DownloadJobStatus.stalled.value,
            error_message='No seeders',
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'Oppenheimer.2023.1080p.WEB-DL',
            'hash': 'aabbccdd',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/complete/Oppenheimer.2023.1080p.WEB-DL',
            'save_path': '/downloads/complete/Oppenheimer.2023.1080p.WEB-DL',
            'added_on': 1,
        }])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _dj.error_message = None
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_scan_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 1
        assert dj.status == DownloadJobStatus.complete.value
        assert dj.error_message is None
        assert imported_paths == ['/downloads/complete/Oppenheimer.2023.1080p.WEB-DL']


def test_scan_recovery_skips_active_downloading_jobs(monkeypatch):
    """run_scan_recovery does not touch jobs currently in 'downloading' status."""
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Avatar (2009).mkv',
            release_name='Avatar.2009.1080p.WEB-DL',
            download_hash='11223344',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'Avatar.2009.1080p.WEB-DL',
            'hash': '11223344',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/complete/Avatar.2009.1080p.WEB-DL',
            'save_path': '/downloads/complete/Avatar.2009.1080p.WEB-DL',
            'added_on': 1,
        }])

        # If run_scan_recovery accidentally processes this job it would call
        # _import_file, which would cause an error — patching it to detect that.
        import_called = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._import_file',
            lambda *_a: import_called.append(True),
        )

        summary = run_scan_recovery(db)
        db.refresh(dj)

        # downloading job must be untouched by scan recovery
        assert summary['imported'] == 0
        assert dj.status == DownloadJobStatus.downloading.value
        assert not import_called


def test_check_download_progress_marks_missing_client_item_stalled_without_fallback(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Missing.Client.Item.mkv',
            release_name='Missing.Client.Item.2025.1080p.WEB-DL',
            download_hash='missinghash',
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=False)

        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 0,
            'eta_seconds': None,
            'is_complete': False,
            'is_stalled': False,
            'save_path': None,
            'not_found': True,
        })
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])

        fallback_calls = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._fallback_to_encode',
            lambda *_args, **_kwargs: fallback_calls.append(True),
        )

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.stalled.value
        assert 'removed from qbittorrent' in (dj.error_message or '').lower()
        assert fallback_calls == []


def test_do_search_routes_grab_to_protocol_specific_download_client(monkeypatch):
    from app.services import prowlarr_service
    from app.services.download_monitor_service import _do_search

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Route.Client.By.Protocol.mkv',
            status=DownloadJobStatus.searching.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        prowlarr_stub = SimpleNamespace(enabled=True, host='http://prowlarr', api_key='key')
        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(prowlarr_service, 'search', lambda *_args, **_kw: [{
            'title': 'Route.Client.By.Protocol.2025.1080p.WEB-DL',
            'seeders': 10,
            'size': 1000,
            'protocol': 'usenet',
            'guid': 'guid-1',
            'indexerId': 1,
        }])
        monkeypatch.setattr(prowlarr_service, 'resolve_download_client_id', lambda *_args, **_kw: 42)

        grab_calls = []

        def _fake_grab(_settings, guid, indexer_id, download_client_id=None):
            grab_calls.append({'guid': guid, 'indexer_id': indexer_id, 'download_client_id': download_client_id})
            return {'downloadId': 'NZO12345'}

        monkeypatch.setattr(prowlarr_service, 'grab', _fake_grab)

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        assert grab_calls and grab_calls[0]['download_client_id'] == 42
        assert dj.client_type == 'sabnzbd'
