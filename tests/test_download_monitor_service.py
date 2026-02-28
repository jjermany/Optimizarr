from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job
from app.models.library import DownloadQualityProfileEnum
from app.services.download_monitor_service import (
    _extract_hash_from_release,
    _find_completed_download_match,
    _build_search_query,
    _check_download_progress,
    _import_file,
    _process_searching_jobs,
    _select_best_release,
    download_job_exists_for_source,
    run_download_startup_recovery,
    run_scan_recovery,
)



def _profile(
    quality: DownloadQualityProfileEnum,
    *,
    tone_map_hdr: bool = False,
    hdr_only: bool = False,
):
    return SimpleNamespace(
        target_resolution=1080,
        download_quality_profile=quality.value,
        tone_map_hdr=tone_map_hdr,
        hdr_only=hdr_only,
    )



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


def test_extract_hash_from_release_reads_magnet_btih():
    release = {
        'magnetUrl': 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Example',
    }
    assert _extract_hash_from_release(release) == '0123456789abcdef0123456789abcdef01234567'



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


def test_select_best_release_prefers_higher_priority_indexer_over_more_seeders():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-HIGHSEED',
            'seeders': 800,
            'size': 1400,
            'protocol': 'torrent',
            'indexerId': 100,
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-HIGHPRIORITY',
            'seeders': 50,
            'size': 1500,
            'protocol': 'torrent',
            'indexerId': 200,
        },
    ]
    indexer_by_id = {
        100: {'id': 100, 'name': 'LowPriorityIndexer', 'priority': 25},
        200: {'id': 200, 'name': 'TopPriorityIndexer', 'priority': 1},
    }

    selected = _select_best_release(
        releases,
        _profile(DownloadQualityProfileEnum.web_dl),
        qbt_enabled=True,
        sab_enabled=True,
        indexer_by_id=indexer_by_id,
    )

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEB-DL.x265-HIGHPRIORITY'


def test_select_best_release_can_choose_usenet_when_priority_is_higher():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-TORRENT',
            'seeders': 500,
            'size': 1300,
            'protocol': 'torrent',
            'indexerId': 10,
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-USENET',
            'seeders': 0,
            'size': 1500,
            'protocol': 'usenet',
            'indexerId': 20,
        },
    ]
    indexer_by_id = {
        10: {'id': 10, 'name': 'TorrentIndexer', 'priority': 30},
        20: {'id': 20, 'name': 'UsenetIndexer', 'priority': 1},
    }

    selected = _select_best_release(
        releases,
        _profile(DownloadQualityProfileEnum.web_dl),
        qbt_enabled=True,
        sab_enabled=True,
        indexer_by_id=indexer_by_id,
    )

    assert selected is not None
    assert selected['protocol'] == 'usenet'


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


def test_select_best_release_tone_map_enabled_rejects_hdr():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.HDR10.x265-HDR',
            'seeders': 500,
            'size': 1500,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-SDR',
            'seeders': 10,
            'size': 1200,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(
        releases,
        _profile(DownloadQualityProfileEnum.web_dl, tone_map_hdr=True),
        qbt_enabled=True,
        sab_enabled=True,
    )

    assert selected is not None
    assert 'HDR' not in selected['title']


def test_select_best_release_tone_map_disabled_allows_hdr():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.HDR10.x265-HDR',
            'seeders': 500,
            'size': 1500,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-SDR',
            'seeders': 10,
            'size': 1200,
            'protocol': 'torrent',
        },
    ]

    selected = _select_best_release(
        releases,
        _profile(DownloadQualityProfileEnum.web_dl, tone_map_hdr=False),
        qbt_enabled=True,
        sab_enabled=True,
    )

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEB-DL.HDR10.x265-HDR'


def test_select_best_release_accepts_legacy_webdl_quality_value():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-GROUP',
            'seeders': 100,
            'size': 1500,
            'protocol': 'torrent',
        }
    ]
    legacy_profile = SimpleNamespace(
        target_resolution=1080,
        download_quality_profile='webdl',
        tone_map_hdr=False,
        hdr_only=False,
    )

    selected = _select_best_release(releases, legacy_profile, qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEB-DL.x265-GROUP'


def test_select_best_release_parses_string_boolean_flags():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.HDR10.x265-HDR',
            'seeders': 50,
            'size': 1600,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-SDR',
            'seeders': 80,
            'size': 1500,
            'protocol': 'torrent',
        },
    ]
    profile = SimpleNamespace(
        target_resolution=1080,
        download_quality_profile='any',
        tone_map_hdr='true',
        hdr_only='false',
    )

    selected = _select_best_release(releases, profile, qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEB-DL.x265-SDR'


def test_select_best_release_resolves_conflicting_hdr_policy_by_prioritizing_tonemap_filter():
    releases = [
        {
            'title': 'Movie.2024.1080p.WEB-DL.HDR10.x265-HDR',
            'seeders': 500,
            'size': 1400,
            'protocol': 'torrent',
        },
        {
            'title': 'Movie.2024.1080p.WEB-DL.x265-SDR',
            'seeders': 50,
            'size': 1500,
            'protocol': 'torrent',
        },
    ]
    profile = SimpleNamespace(
        target_resolution=1080,
        download_quality_profile='web_dl',
        tone_map_hdr=True,
        hdr_only=True,
    )

    selected = _select_best_release(releases, profile, qbt_enabled=True, sab_enabled=True)

    assert selected is not None
    assert selected['title'] == 'Movie.2024.1080p.WEB-DL.x265-SDR'


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


def test_download_job_exists_for_source_ignores_stale_complete_without_imported_file():
    source_path = '/media/download-complete-stale.mkv'
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.commit()

        stale_complete = DownloadJob(
            source_file_path=source_path,
            status=DownloadJobStatus.complete.value,
            imported_file_path='/path/that/does/not/exist/output.mkv',
        )
        db.add(stale_complete)
        db.commit()

        assert download_job_exists_for_source(db, source_path) is False


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


def test_find_completed_download_match_uses_title_year_fallback_keys(tmp_path):
    completed_root = tmp_path / 'complete'
    candidate = completed_root / 'Doctor Strange (2016) IMAX (1080p DSNP WEB-DL x265 HEVC 10bit EAC3 5.1 Silence)'
    candidate.mkdir(parents=True)
    (candidate / 'Doctor.Strange.2016.1080p.mkv').write_bytes(b'x')

    dj = DownloadJob(
        source_file_path='/data/media/movies/Doctor Strange (2016) {imdb-tt1211837}/'
        'Doctor Strange (2016) {imdb-tt1211837} [Bluray-2160p][HDR10][AC3 5.1][x265]-BHDStudio.mp4',
        release_name=None,
        client_type='qbittorrent',
        status=DownloadJobStatus.downloading.value,
    )

    match = _find_completed_download_match(dj, str(completed_root))
    assert match == str(candidate)


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


def test_check_download_progress_recovers_hash_when_qbit_title_is_renamed(monkeypatch):
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
            # Release name from indexer/Prowlarr; qBit may rename this after metadata fetch.
            release_name='The.Gorge.2025.1080p.WEB-DL-OLDNAME',
            download_hash=None,
            client_type='qbittorrent',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=False)

        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            # Deliberately does NOT equal release_name exactly.
            'name': 'The.Gorge.2025.1080p.WEB-DL-GROUP',
            'hash': 'renamedhash',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/The.Gorge.2025.1080p.WEB-DL-GROUP',
            'added_on': 1,
        }])
        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 100,
            'is_complete': True,
            'is_stalled': False,
            'save_path': '/downloads/The.Gorge.2025.1080p.WEB-DL-GROUP',
        })
        monkeypatch.setattr(download_client_service, 'tag_qbt_torrent', lambda *_args, **_kwargs: True)

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.download_hash == 'renamedhash'
        assert imported_paths == ['/downloads/The.Gorge.2025.1080p.WEB-DL-GROUP']


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


def test_startup_recovery_imports_importing_job_completed_in_qbit(monkeypatch):
    """A job left in importing during restart must be recovered and completed."""
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
            download_hash='feedface',
            client_type='qbittorrent',
            status=DownloadJobStatus.importing.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [{
            'name': 'The.Gorge.2025.1080p.WEB-DL',
            'hash': 'feedface',
            'state': 'uploading',
            'progress': 1.0,
            'content_path': '/downloads/complete/The.Gorge.2025.1080p.WEB-DL',
            'save_path': '/downloads/complete/The.Gorge.2025.1080p.WEB-DL',
            'added_on': 1,
        }])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 1
        assert summary['reset_to_searching'] == 0
        assert dj.status == DownloadJobStatus.complete.value
        assert imported_paths == ['/downloads/complete/The.Gorge.2025.1080p.WEB-DL']


def test_startup_recovery_keeps_hashless_qbit_job_downloading_when_unmatched(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Doctor Strange (2016).mkv',
            release_name='Some.Release.Name',
            download_hash=None,
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
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 0
        assert summary['reset_to_searching'] == 0
        assert dj.status == DownloadJobStatus.downloading.value


def test_startup_recovery_removes_waiting_encode_placeholder_for_completed_download(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        queued = Job(
            input_path='/media/Blade Runner 2049 (2017).mkv',
            status='queued',
            library_id=library.id,
            progress_percent=0,
        )
        db.add(queued)
        db.commit()
        db.refresh(queued)

        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Blade Runner 2049 (2017).mkv',
            status=DownloadJobStatus.complete.value,
            imported_file_path='/media/Blade Runner 2049 (2017)-1080p.mkv',
        )
        db.add(dj)
        db.commit()

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))

        summary = run_download_startup_recovery(db)
        removed = db.query(Job).filter(Job.id == queued.id).first()

        assert summary['imported'] == 0
        assert summary['reset_to_searching'] == 0
        assert summary['linked_jobs'] == 1
        assert removed is None


def test_startup_recovery_adopts_queue_when_stale_complete_download_row_exists(monkeypatch):
    imported_paths = []
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        queued = Job(
            input_path='/media/Doctor Strange (2016).mkv',
            status='queued',
            library_id=library.id,
            progress_percent=0,
        )
        db.add(queued)
        db.commit()

        stale = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Doctor Strange (2016).mkv',
            status=DownloadJobStatus.complete.value,
            imported_file_path='/does/not/exist/doctor-strange-1080p.mkv',
        )
        db.add(stale)
        db.commit()

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])
        monkeypatch.setattr(
            'app.services.download_monitor_service._find_completed_download_match',
            lambda job, _root: '/downloads/complete/Doctor Strange (2016) IMAX (1080p DSNP WEB-DL x265 HEVC 10bit EAC3 5.1 Silence)'
            if job.source_file_path == '/media/Doctor Strange (2016).mkv'
            else None,
        )

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _dj.imported_file_path = '/media/Doctor Strange (2016)-1080p.mkv'
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(queued)

        assert summary.get('adopted_queue_jobs', 0) == 1
        assert queued.status == 'complete'
        assert imported_paths == ['/downloads/complete/Doctor Strange (2016) IMAX (1080p DSNP WEB-DL x265 HEVC 10bit EAC3 5.1 Silence)']


def test_startup_recovery_adopts_untracked_completed_sab_item(monkeypatch):
    imported_paths = []
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        queued = Job(
            input_path='/media/The Gorge (2025).mkv',
            status='queued',
            library_id=library.id,
            progress_percent=0,
        )
        db.add(queued)
        db.commit()

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_sab_completed_history_items', lambda _s: [
            {
                'nzo_id': 'SABNZBD_NZO_abc123',
                'name': 'The.Gorge.2025.1080p.WEB-DL',
                'save_path': '/data/complete/usenet/The.Gorge.2025.1080p.WEB-DL',
            }
        ])

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _dj.imported_file_path = '/media/The Gorge (2025)-1080p.mkv'
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(queued)
        adopted = (
            db.query(DownloadJob)
            .filter(DownloadJob.source_file_path == '/media/The Gorge (2025).mkv')
            .order_by(DownloadJob.id.desc())
            .first()
        )

        assert summary.get('adopted_queue_jobs', 0) == 1
        assert queued.status == 'complete'
        assert imported_paths == ['/data/complete/usenet/The.Gorge.2025.1080p.WEB-DL']
        assert adopted is not None
        assert adopted.client_type == 'sabnzbd'
        assert adopted.download_hash == 'SABNZBD_NZO_abc123'


def test_startup_recovery_skips_untracked_completed_sab_item_on_profile_mismatch(monkeypatch):
    import_calls = []
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        queued = Job(
            input_path='/media/The Gorge (2025).mkv',
            status='queued',
            library_id=library.id,
            progress_percent=0,
        )
        db.add(queued)
        db.commit()

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_sab_completed_history_items', lambda _s: [
            {
                'nzo_id': 'SABNZBD_NZO_bad',
                'name': 'The.Gorge.2025.720p.WEB-DL',
                'save_path': '/data/complete/usenet/The.Gorge.2025.720p.WEB-DL',
            }
        ])
        monkeypatch.setattr(
            'app.services.download_monitor_service._import_file',
            lambda *_args, **_kwargs: import_calls.append(True),
        )

        summary = run_download_startup_recovery(db)
        adopted = (
            db.query(DownloadJob)
            .filter(DownloadJob.source_file_path == '/media/The Gorge (2025).mkv')
            .all()
        )
        db.refresh(queued)

        assert summary.get('adopted_queue_jobs', 0) == 0
        assert import_calls == []
        assert adopted == []
        assert queued.status == 'queued'


def test_startup_recovery_imports_searching_job_via_source_name_match(monkeypatch):
    imported_paths = []

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Doctor Strange (2016).mkv',
            status=DownloadJobStatus.searching.value,
            release_name=None,
            download_hash=None,
            client_type='qbittorrent',
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))
        monkeypatch.setattr(download_client_service, 'get_qbt_default_save_path', lambda _q: '/downloads/complete')
        monkeypatch.setattr(download_client_service, 'get_all_qbt_tagged_torrents', lambda _q: [])
        monkeypatch.setattr(download_client_service, 'get_all_qbt_torrents', lambda _q: [])
        monkeypatch.setattr(
            'app.services.download_monitor_service._find_completed_download_match',
            lambda job, _root: '/downloads/complete/Doctor Strange (2016) IMAX (1080p DSNP WEB-DL x265 HEVC 10bit EAC3 5.1 Silence)' if job.id == dj.id else None,
        )

        def _fake_import(_db, _dj, save_path, *_args):
            imported_paths.append(save_path)
            _dj.status = DownloadJobStatus.complete.value
            _db.commit()

        monkeypatch.setattr('app.services.download_monitor_service._import_file', _fake_import)

        summary = run_download_startup_recovery(db)
        db.refresh(dj)

        assert summary['imported'] == 1
        assert summary['reset_to_searching'] == 0
        assert dj.status == DownloadJobStatus.complete.value
        assert imported_paths == ['/downloads/complete/Doctor Strange (2016) IMAX (1080p DSNP WEB-DL x265 HEVC 10bit EAC3 5.1 Silence)']


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
        monkeypatch.setattr(prowlarr_service, 'get_indexers', lambda *_args, **_kw: [])

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


def test_process_searching_jobs_retries_existing_searching_job(monkeypatch):
    from app.services import download_client_service, prowlarr_service

    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Doctor Strange (2016).mkv',
            status=DownloadJobStatus.searching.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        monkeypatch.setattr(prowlarr_service, 'get_or_create_prowlarr_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))
        monkeypatch.setattr(download_client_service, 'get_or_create_sab_settings', lambda _db: SimpleNamespace(enabled=False))

        calls = []

        def _fake_do_search(_db, job, *_args):
            calls.append(job.id)

        monkeypatch.setattr('app.services.download_monitor_service._do_search', _fake_do_search)

        _process_searching_jobs(db)
        assert calls == [dj.id]


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


def test_check_download_progress_missing_client_item_retries_without_fallback(monkeypatch):
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

        assert dj.status == DownloadJobStatus.searching.value
        assert 'removed from qbittorrent' in (dj.error_message or '').lower()
        assert 'retrying 1/5' in (dj.error_message or '').lower()
        assert fallback_calls == []


def test_check_download_progress_stalled_retries_search_before_fallback(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Stalled.Retry.Item.mkv',
            release_name='Stalled.Retry.Item.2025.1080p.WEB-DL',
            indexer_id=42,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
            retry_count=0,
            max_retries=5,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=False)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 0,
            'eta_seconds': None,
            'is_complete': False,
            'is_stalled': True,
            'save_path': None,
            'not_found': False,
        })
        monkeypatch.setattr(download_client_service, 'set_sab_category', lambda *_args, **_kwargs: True)

        fallback_calls = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._fallback_to_encode',
            lambda *_args, **_kwargs: fallback_calls.append(True),
        )

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.searching.value
        assert dj.retry_count == 1
        assert 'retrying 1/5' in (dj.error_message or '')
        assert fallback_calls == []
        failed_keys = json.loads(dj.failed_release_keys or '[]')
        assert failed_keys


def test_check_download_progress_stalled_exhausted_retries_falls_back(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Stalled.Exhausted.Item.mkv',
            release_name='Stalled.Exhausted.Item.2025.1080p.WEB-DL',
            indexer_id=77,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
            retry_count=5,
            max_retries=5,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=False)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 0,
            'eta_seconds': None,
            'is_complete': False,
            'is_stalled': True,
            'save_path': None,
            'not_found': False,
        })
        monkeypatch.setattr(download_client_service, 'set_sab_category', lambda *_args, **_kwargs: True)

        fallback_calls = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._fallback_to_encode',
            lambda *_args, **_kwargs: fallback_calls.append(True),
        )

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.failed.value
        assert 'retries exhausted' in (dj.error_message or '')
        assert fallback_calls == [True]


def test_check_download_progress_stalled_exhausted_usenet_retries_switches_to_torrent_search(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/Usenet.Switch.To.Torrent.mkv',
            release_name='Usenet.Switch.To.Torrent.2025.1080p.WEB-DL',
            indexer_id=21,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
            retry_count=5,
            max_retries=5,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: {
            'progress_percent': 0,
            'eta_seconds': None,
            'is_complete': False,
            'is_stalled': True,
            'save_path': None,
            'not_found': False,
        })
        monkeypatch.setattr(download_client_service, 'set_sab_category', lambda *_args, **_kwargs: True)
        monkeypatch.setattr(download_client_service, 'get_or_create_qbt_settings', lambda _db: SimpleNamespace(enabled=True))

        fallback_calls = []
        monkeypatch.setattr(
            'app.services.download_monitor_service._fallback_to_encode',
            lambda *_args, **_kwargs: fallback_calls.append(True),
        )

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.searching.value
        assert dj.retry_count == 0
        assert 'switching to torrent search' in (dj.error_message or '')
        failed_keys = set(json.loads(dj.failed_release_keys or '[]'))
        assert 'protocol:usenet' in failed_keys
        assert fallback_calls == []


def test_check_download_progress_sab_hashless_waits_for_nzo_recovery(monkeypatch):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        dj = DownloadJob(
            library_id=library.id,
            source_file_path='/media/The.Perfect.Neighbor.2025.mkv',
            release_name='The.Perfect.Neighbor.2025.1080p.WEB-DL.DD.5.1.H.264-playWEB',
            download_hash=None,
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        qbt = SimpleNamespace(enabled=False)
        sab = SimpleNamespace(enabled=True)

        # NZO id not visible in SAB yet — job should remain downloading and wait.
        monkeypatch.setattr(download_client_service, 'find_sab_nzo_for_release', lambda *_args: '')
        status_calls = []
        monkeypatch.setattr(download_client_service, 'get_download_status', lambda *_args: status_calls.append(True) or {})

        _check_download_progress(db, dj, qbt, sab)
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.downloading.value
        assert dj.download_hash is None
        assert status_calls == []


def test_do_search_defers_download_client_routing_to_prowlarr(monkeypatch):
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
        monkeypatch.setattr(prowlarr_service, 'get_indexers', lambda *_args, **_kw: [{'id': 1, 'name': 'TestIndexer', 'priority': 1}])
        grab_calls = []
        sab_category_calls = []

        def _fake_grab(_settings, guid, indexer_id, download_client_id=None):
            grab_calls.append({'guid': guid, 'indexer_id': indexer_id, 'download_client_id': download_client_id})
            return {'downloadId': 'NZO12345'}

        monkeypatch.setattr(prowlarr_service, 'grab', _fake_grab)
        monkeypatch.setattr(
            download_client_service,
            'set_sab_category',
            lambda _sab, nzo_id, category='optimizarr': sab_category_calls.append({'nzo_id': nzo_id, 'category': category}) or True,
        )

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        assert grab_calls and grab_calls[0]['download_client_id'] is None
        assert dj.client_type == 'sabnzbd'
        assert dj.indexer_id == 1
        assert dj.indexer_name == 'TestIndexer'
        assert sab_category_calls and sab_category_calls[0]['category'] == 'optimizarr'


def test_do_search_skips_previously_failed_release_keys(monkeypatch):
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
            source_file_path='/media/Skip.Failed.Release.mkv',
            status=DownloadJobStatus.searching.value,
            failed_release_keys=json.dumps(['guid:bad-guid']),
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        prowlarr_stub = SimpleNamespace(enabled=True, host='http://prowlarr', api_key='key')
        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(prowlarr_service, 'search', lambda *_args, **_kw: [
            {
                'title': 'Skip.Failed.Release.2025.1080p.WEB-DL-BAD',
                'seeders': 500,
                'size': 1100,
                'protocol': 'usenet',
                'guid': 'bad-guid',
                'indexerId': 1,
            },
            {
                'title': 'Skip.Failed.Release.2025.1080p.WEB-DL-GOOD',
                'seeders': 10,
                'size': 1200,
                'protocol': 'usenet',
                'guid': 'good-guid',
                'indexerId': 1,
            },
        ])
        monkeypatch.setattr(prowlarr_service, 'get_indexers', lambda *_args, **_kw: [{'id': 1, 'name': 'TestIndexer', 'priority': 1}])

        grabbed = []
        monkeypatch.setattr(
            prowlarr_service,
            'grab',
            lambda *_args, **_kwargs: (grabbed.append(_args[1]) or {'downloadId': 'NZO_OK'}),
        )
        monkeypatch.setattr(download_client_service, 'set_sab_category', lambda *_args, **_kwargs: True)

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        assert grabbed == ['good-guid']
        assert dj.status == DownloadJobStatus.downloading.value


def test_do_search_excludes_usenet_when_protocol_marker_present(monkeypatch):
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
            source_file_path='/media/Protocol.Exclusion.Test.mkv',
            status=DownloadJobStatus.searching.value,
            failed_release_keys=json.dumps(['protocol:usenet']),
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        prowlarr_stub = SimpleNamespace(enabled=True, host='http://prowlarr', api_key='key')
        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(prowlarr_service, 'search', lambda *_args, **_kw: [
            {
                'title': 'Protocol.Exclusion.Test.2025.1080p.WEB-DL-USENET',
                'seeders': 0,
                'size': 1100,
                'protocol': 'usenet',
                'guid': 'usenet-guid',
                'indexerId': 1,
            },
            {
                'title': 'Protocol.Exclusion.Test.2025.1080p.WEB-DL-TORRENT',
                'seeders': 2,
                'size': 1300,
                'protocol': 'torrent',
                'guid': 'torrent-guid',
                'indexerId': 2,
            },
        ])
        monkeypatch.setattr(
            prowlarr_service,
            'get_indexers',
            lambda *_args, **_kw: [
                {'id': 1, 'name': 'UsenetIndexer', 'priority': 1},
                {'id': 2, 'name': 'TorrentIndexer', 'priority': 20},
            ],
        )

        grabbed = []
        monkeypatch.setattr(
            prowlarr_service,
            'grab',
            lambda *_args, **_kwargs: (grabbed.append(_args[1]) or {'downloadId': 'TORRENT_HASH_123'}),
        )
        monkeypatch.setattr(download_client_service, 'tag_qbt_torrent', lambda *_args, **_kwargs: True)

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        assert grabbed == ['torrent-guid']
        assert dj.client_type == 'qbittorrent'
        assert dj.status == DownloadJobStatus.downloading.value


def test_do_search_rejects_unrelated_episode_title_for_single_word_movie(monkeypatch):
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
            source_file_path='/media/Wicked (2024).mkv',
            status=DownloadJobStatus.searching.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        prowlarr_stub = SimpleNamespace(enabled=True, host='http://prowlarr', api_key='key')
        qbt = SimpleNamespace(enabled=True)
        sab = SimpleNamespace(enabled=True)

        monkeypatch.setattr(prowlarr_service, 'search', lambda *_args, **_kw: [
            {
                'title': 'Cinderella.Game.2024.S01E75.The.Wicked.Exposure.1080p.VIU.WEB-DL.AAC2',
                'seeders': 1000,
                'size': 1200,
                'protocol': 'usenet',
                'guid': 'bad-episode-guid',
                'indexerId': 1,
            },
            {
                'title': 'Wicked.2024.1080p.WEB-DL.DDP5.1.H264',
                'seeders': 10,
                'size': 2200,
                'protocol': 'usenet',
                'guid': 'good-movie-guid',
                'indexerId': 1,
            },
        ])
        monkeypatch.setattr(prowlarr_service, 'get_indexers', lambda *_args, **_kw: [{'id': 1, 'name': 'TestIndexer', 'priority': 1}])

        grabbed = []
        monkeypatch.setattr(
            prowlarr_service,
            'grab',
            lambda *_args, **_kwargs: (grabbed.append(_args[1]) or {'downloadId': 'NZO_WICKED_OK'}),
        )
        monkeypatch.setattr(download_client_service, 'set_sab_category', lambda *_args, **_kwargs: True)

        _do_search(db, dj, prowlarr_stub, qbt, sab)
        db.refresh(dj)

        assert grabbed == ['good-movie-guid']
        assert dj.release_name == 'Wicked.2024.1080p.WEB-DL.DDP5.1.H264'


def test_import_file_sab_removes_source_video_from_completed_directory(monkeypatch, tmp_path):
    with SessionLocal() as db:
        db.query(DownloadJob).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = _seed_library_with_profile(db)
        source = tmp_path / 'library' / 'The Gorge (2025).mkv'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'source')

        completed_dir = tmp_path / 'complete' / 'usenet' / 'The.Gorge.2025.1080p.WEB-DL'
        completed_dir.mkdir(parents=True)
        completed_video = completed_dir / 'The.Gorge.2025.1080p.WEB-DL.mkv'
        completed_video.write_bytes(b'downloaded-video')

        dj = DownloadJob(
            library_id=library.id,
            source_file_path=str(source),
            release_name='The.Gorge.2025.1080p.WEB-DL',
            download_hash='SABNZBD_NZO_abc123',
            client_type='sabnzbd',
            status=DownloadJobStatus.downloading.value,
        )
        db.add(dj)
        db.commit()
        db.refresh(dj)

        deleted_history = []
        monkeypatch.setattr(
            download_client_service,
            'delete_sab_history',
            lambda _sab, nzo_id: deleted_history.append(nzo_id),
        )

        _import_file(
            db,
            dj,
            str(completed_dir),
            library,
            library.profile,
            SimpleNamespace(enabled=False),
            SimpleNamespace(enabled=True),
        )
        db.refresh(dj)

        assert dj.status == DownloadJobStatus.complete.value
        assert dj.imported_file_path is not None
        assert Path(dj.imported_file_path).exists()
        assert not completed_video.exists()
        assert deleted_history == ['SABNZBD_NZO_abc123']
