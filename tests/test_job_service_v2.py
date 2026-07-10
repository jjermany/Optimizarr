from app.core.database import SessionLocal
from app.models.job import Job
from app.models.download_job import DownloadJob
from app.models.library import Library
from app.services.job_service import (
    find_existing_target_artifact_for_identity,
    has_completed_job_for_identity,
    media_identity_key,
)
from pathlib import Path


def test_media_identity_key_strips_release_info():
    assert media_identity_key('/media/movies/Example Movie (2023)/Example Movie (2023) 1080p BluRay.mkv') == 'movie:examplemovie:2023'
    assert media_identity_key('/media/movies/Example Movie (2023)/Example Movie (2023) 2160p WEB-DL.mkv') == 'movie:examplemovie:2023'
    assert media_identity_key('/media/movies/Example Movie (2023)/Example Movie (2023) [2160p WEB-DL].mkv') == 'movie:examplemovie:2023'
    assert media_identity_key('/media/movies/Example Movie (2023)/Example.Movie.2023.1080p.BluRay.x264-GROUP.mkv') == 'movie:examplemovie:2023'
    assert media_identity_key('/media/tv/Example Show/Season 01/Example Show S01E01 1080p.mkv') == 'tv:exampleshow:s01:e001'


def test_find_existing_target_artifact_for_identity_matches_different_id_tag(tmp_path):
    existing = tmp_path / (
        'Companion (2025) {imdb-tt26584495} [Bluray-2160p][DV HDR10Plus]'
        '[TrueHD Atmos 7.1][x265]-SEV-1080p.mkv'
    )
    source = tmp_path / (
        'Companion (2025) {tmdb-1084199} [MA][WEBDL-2160p][DV HDR10]'
        '[EAC3 5.1][h265]-DVT.mkv'
    )
    existing.touch()
    source.touch()

    assert find_existing_target_artifact_for_identity(str(source), 1080, '-1080p') == existing


def test_find_existing_target_artifact_matches_yearless_same_folder_movie(tmp_path):
    existing = tmp_path / 'Roommates {tmdb-123} [Bluray]-1080p.mkv'
    source = tmp_path / 'Roommates {imdb-tt123} [WEBDL]-2160p.mkv'
    existing.touch()
    source.touch()

    assert media_identity_key(str(source)) is None
    assert find_existing_target_artifact_for_identity(str(source), 1080, '-optimized') == existing


def test_find_existing_target_artifact_probes_unlabelled_same_identity(monkeypatch, tmp_path):
    existing = tmp_path / 'Roommates (2026) optimized.mkv'
    source = tmp_path / 'Roommates (2026) source.mkv'
    existing.touch()
    source.touch()
    monkeypatch.setattr(
        'app.services.job_service.optimization_service.probe_video_height',
        lambda path: 1080 if path == str(existing) else 2160,
    )

    assert find_existing_target_artifact_for_identity(str(source), 1080, '-1080p') == existing


def test_has_completed_job_for_identity(tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(DownloadJob).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        # Test with a completed encode job
        output_path_encode = tmp_path / 'Example.Movie.2023-optimized.mkv'
        output_path_encode.touch()
        completed_encode_job = Job(
            input_path='/media/movies/Example.Movie.2023.mkv',
            library_id=library.id,
            status='complete',
            output_path=str(output_path_encode)
        )
        db.add(completed_encode_job)
        db.commit()

        assert has_completed_job_for_identity(db, '/media/movies/Example.Movie.2023.different.release.mkv', library.id) is True

        # Test with a completed download job
        output_path_download = tmp_path / 'Example.Movie.2024.mkv'
        output_path_download.touch()
        completed_download_job = DownloadJob(
            source_file_path='/media/movies/Example.Movie.2024.mkv',
            library_id=library.id,
            status='complete',
            imported_file_path=str(output_path_download)
        )
        db.add(completed_download_job)
        db.commit()
        
        assert has_completed_job_for_identity(db, '/media/movies/Example.Movie.2024.different.release.mkv', library.id) is True

        # Test with no completed job
        assert has_completed_job_for_identity(db, '/media/movies/Another.Movie.2023.mkv', library.id) is False

        # Test with completed job but missing output file
        output_path_encode.unlink()
        assert has_completed_job_for_identity(db, '/media/movies/Example.Movie.2023.different.release.mkv', library.id) is False
