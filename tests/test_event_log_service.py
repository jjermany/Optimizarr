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


def test_clear_events_removes_all_logs():
    with SessionLocal() as db:
        db.query(EventLog).delete()
        db.commit()

        event_log_service.record_event(db, 'one', 'First event')
        event_log_service.record_event(db, 'two', 'Second event')

        assert event_log_service.clear_events(db) == 2
        assert db.query(EventLog).count() == 0
