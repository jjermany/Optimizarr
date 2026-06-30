from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.services.discovery_service import _prepare_probe_candidate
from pathlib import Path

def test_prepare_probe_candidate_skips_if_completed_job_exists(tmp_path):
    with SessionLocal() as db:
        db.query(Job).delete()
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library_path = tmp_path / "movies"
        library_path.mkdir()

        library = Library(name='Movies', path=str(library_path), enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, output_suffix='-optimized')
        db.add(profile)
        db.commit()
        db.refresh(profile)

        # Create a completed job in the database
        output_file = library_path / "Example Movie (2023)-optimized.mkv"
        output_file.touch()
        completed_job = Job(
            input_path=str(library_path / "Example Movie (2023).mkv"),
            library_id=library.id,
            status='complete',
            output_path=str(output_file)
        )
        db.add(completed_job)
        db.commit()

        # This file should be skipped because a completed version exists
        new_media_file = library_path / "Example Movie (2023) 2160p.mkv"
        new_media_file.touch()

        candidate = _prepare_probe_candidate(db, new_media_file, library, profile)
        assert candidate is None

        # This file should be processed as it's a different movie
        another_movie_file = library_path / "Another Movie (2023).mkv"
        another_movie_file.touch()
        candidate = _prepare_probe_candidate(db, another_movie_file, library, profile)
        assert candidate is not None
        assert candidate.media_file == another_movie_file
