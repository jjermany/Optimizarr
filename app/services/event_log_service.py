from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.event_log import EventLog

MAX_LOG_ROWS = 1000


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat()
    return str(value)


def record_event(
    db: Session,
    event_type: str,
    message: str,
    *,
    severity: str = 'info',
    details: dict[str, Any] | None = None,
) -> EventLog:
    log = EventLog(
        event_type=event_type,
        severity=severity,
        message=message,
        details_json=json.dumps(details or {}, sort_keys=True, default=_json_default),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    _trim_old_events(db)
    return log


def list_events(db: Session, *, limit: int = 200) -> list[EventLog]:
    bounded_limit = max(1, min(int(limit), MAX_LOG_ROWS))
    return (
        db.query(EventLog)
        .order_by(EventLog.created_at.desc(), EventLog.id.desc())
        .limit(bounded_limit)
        .all()
    )


def _trim_old_events(db: Session) -> None:
    stale_ids = [
        row[0]
        for row in (
            db.query(EventLog.id)
            .order_by(EventLog.created_at.desc(), EventLog.id.desc())
            .offset(MAX_LOG_ROWS)
            .all()
        )
    ]
    if not stale_ids:
        return
    db.query(EventLog).filter(EventLog.id.in_(stale_ids)).delete(synchronize_session=False)
    db.commit()
