from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.library import DownloadQualityProfileEnum
from app.services.download_monitor_service import _select_best_release, download_job_exists_for_source



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
from app.services.download_monitor_service import _check_download_progress, run_download_startup_recovery


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
