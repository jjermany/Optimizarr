from app.core.database import SessionLocal
from app.models.event_log import EventLog
from app.services import event_log_service


def test_record_event_once_reuses_recent_identical_event():
    with SessionLocal() as db:
        db.query(EventLog).delete()
        db.commit()

        first = event_log_service.record_event_once(
            db,
            'recovery_summary',
            'Startup recovery completed',
            details={'trigger': 'startup', 'recovered_jobs': 0},
        )
        second = event_log_service.record_event_once(
            db,
            'recovery_summary',
            'Startup recovery completed',
            details={'trigger': 'startup', 'recovered_jobs': 0},
        )

        assert second.id == first.id
        assert db.query(EventLog).count() == 1


def test_record_event_once_keeps_changed_details():
    with SessionLocal() as db:
        db.query(EventLog).delete()
        db.commit()

        first = event_log_service.record_event_once(
            db,
            'recovery_summary',
            'Startup recovery completed',
            details={'trigger': 'startup', 'recovered_jobs': 0},
        )
        second = event_log_service.record_event_once(
            db,
            'recovery_summary',
            'Startup recovery completed',
            details={'trigger': 'startup', 'recovered_jobs': 1},
        )

        assert second.id != first.id
        assert db.query(EventLog).count() == 2
