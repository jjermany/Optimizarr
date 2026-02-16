from datetime import datetime, timedelta

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.services.job_service import create_job, prune_job_history


def test_create_job_stores_profile_snapshot():
    with SessionLocal() as db:
        db.query(LibraryProfile).delete()
        db.query(Library).delete()
        db.commit()

        library = Library(name='Movies', path='/media/movies', enabled=True)
        db.add(library)
        db.commit()
        db.refresh(library)

        profile = LibraryProfile(library_id=library.id, codec='av1', av1_fallback_codec='h264', output_suffix='-opt')
        db.add(profile)
        db.commit()
        db.refresh(profile)

        job = create_job(db, '/media/movies/title.mkv', library_id=library.id, profile=profile)
        assert job.library_id == library.id
        assert job.profile_snapshot_json is not None
        assert '"codec": "av1"' in job.profile_snapshot_json
        assert '"output_suffix": "-opt"' in job.profile_snapshot_json


def test_prune_job_history_removes_stale_terminal_jobs():
    with SessionLocal() as db:
        stale_terminal = Job(
            input_path='/media/old.mkv',
            status='complete',
            completed_at=datetime.utcnow() - timedelta(days=40),
        )
        fresh_terminal = Job(
            input_path='/media/new.mkv',
            status='failed',
            completed_at=datetime.utcnow() - timedelta(days=1),
        )
        stale_active = Job(
            input_path='/media/running.mkv',
            status='running',
            completed_at=datetime.utcnow() - timedelta(days=40),
        )
        db.add_all([stale_terminal, fresh_terminal, stale_active])
        db.commit()

        deleted = prune_job_history(db, retention_days=30)
        assert deleted >= 1

        remaining_ids = {job.id for job in db.query(Job).all()}
        assert stale_terminal.id not in remaining_ids
        assert fresh_terminal.id in remaining_ids
        assert stale_active.id in remaining_ids

        db.delete(fresh_terminal)
        db.delete(stale_active)
        db.commit()
