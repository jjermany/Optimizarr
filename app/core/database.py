import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = Path(os.getenv('PLEX_OPTIMIZER_DB_PATH', '/config/plex_optimizer.db'))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_columns(connection, table_name: str) -> set[str]:
    rows = connection.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
    return {row[1] for row in rows}


def _add_column_if_missing(connection, table_name: str, column_name: str, column_ddl: str) -> None:
    existing_columns = _table_columns(connection, table_name)
    if column_name not in existing_columns:
        connection.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_ddl}'))


def init_db() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        _add_column_if_missing(
            connection,
            'settings',
            'history_retention_days',
            'history_retention_days INTEGER NOT NULL DEFAULT 30',
        )
        _add_column_if_missing(
            connection,
            'settings',
            'global_quiet_enabled',
            'global_quiet_enabled BOOLEAN NOT NULL DEFAULT 0',
        )
        _add_column_if_missing(
            connection,
            'settings',
            'global_quiet_start_hour',
            'global_quiet_start_hour INTEGER NOT NULL DEFAULT 22',
        )
        _add_column_if_missing(
            connection,
            'settings',
            'global_quiet_end_hour',
            'global_quiet_end_hour INTEGER NOT NULL DEFAULT 6',
        )


        _add_column_if_missing(
            connection,
            'settings',
            'auto_discovery_enabled',
            'auto_discovery_enabled BOOLEAN NOT NULL DEFAULT 1',
        )
        _add_column_if_missing(
            connection,
            'settings',
            'discovery_method',
            "discovery_method VARCHAR(8) NOT NULL DEFAULT 'interval'",
        )
        _add_column_if_missing(
            connection,
            'settings',
            'discovery_interval_minutes',
            'discovery_interval_minutes INTEGER NOT NULL DEFAULT 30',
        )



        _add_column_if_missing(
            connection,
            'settings',
            'min_free_gb',
            'min_free_gb INTEGER NOT NULL DEFAULT 25',
        )

        _add_column_if_missing(
            connection,
            'settings',
            'workspace_root',
            "workspace_root VARCHAR(512) NOT NULL DEFAULT '/cache/workspaces'",
        )
        _add_column_if_missing(
            connection,
            'settings',
            'requeue_interrupted_jobs',
            'requeue_interrupted_jobs BOOLEAN NOT NULL DEFAULT 1',
        )
        _add_column_if_missing(
            connection,
            'settings',
            'cleanup_workspaces_on_startup',
            'cleanup_workspaces_on_startup BOOLEAN NOT NULL DEFAULT 1',
        )

        _add_column_if_missing(
            connection,
            'notification_settings',
            'notify_on_job_interrupted',
            'notify_on_job_interrupted BOOLEAN NOT NULL DEFAULT 1',
        )
        _add_column_if_missing(
            connection,
            'notification_settings',
            'notify_on_low_disk_pause',
            'notify_on_low_disk_pause BOOLEAN NOT NULL DEFAULT 1',
        )
        _add_column_if_missing(
            connection,
            'notification_settings',
            'notify_on_recovery_ran',
            'notify_on_recovery_ran BOOLEAN NOT NULL DEFAULT 1',
        )

        _add_column_if_missing(connection, 'jobs', 'library_id', 'library_id INTEGER')
        _add_column_if_missing(connection, 'jobs', 'profile_snapshot_json', 'profile_snapshot_json TEXT')
        _add_column_if_missing(connection, 'jobs', 'encoder_used', 'encoder_used VARCHAR(64)')
        _add_column_if_missing(connection, 'jobs', 'codec_used', 'codec_used VARCHAR(32)')
        _add_column_if_missing(connection, 'jobs', 'hwaccel_used', 'hwaccel_used BOOLEAN')
        _add_column_if_missing(connection, 'jobs', 'used_fallback', 'used_fallback BOOLEAN')
        _add_column_if_missing(connection, 'jobs', 'fallback_reason', 'fallback_reason TEXT')
        _add_column_if_missing(connection, 'jobs', 'error_message', 'error_message TEXT')
        _add_column_if_missing(connection, 'jobs', 'source_resolution', 'source_resolution INTEGER')
        _add_column_if_missing(connection, 'jobs', 'source_is_hdr', 'source_is_hdr BOOLEAN')
        _add_column_if_missing(connection, 'jobs', 'resume_position_seconds', 'resume_position_seconds REAL')

        _add_column_if_missing(connection, 'library_profiles', 'minimum_source_resolution', 'minimum_source_resolution INTEGER NOT NULL DEFAULT 2160')
        _add_column_if_missing(connection, 'library_profiles', 'schedule_policy', "schedule_policy VARCHAR(14) NOT NULL DEFAULT 'finish_current'")
        _add_column_if_missing(connection, 'library_profiles', 'schedule_enabled', 'schedule_enabled BOOLEAN NOT NULL DEFAULT 1')

        _add_column_if_missing(
            connection,
            'library_profiles',
            'output_conflict_policy',
            "output_conflict_policy VARCHAR(9) NOT NULL DEFAULT 'skip'",
        )
        _add_column_if_missing(
            connection,
            'library_profiles',
            'preferred_video_encoder',
            "preferred_video_encoder VARCHAR(8) NOT NULL DEFAULT 'auto'",
        )
        _add_column_if_missing(
            connection,
            'library_profiles',
            'tone_map_hdr',
            'tone_map_hdr BOOLEAN NOT NULL DEFAULT 0',
        )

        # plex_settings is created fresh by Base.metadata.create_all above.
        _add_column_if_missing(
            connection,
            'library_profiles',
            'plex_library_id',
            'plex_library_id VARCHAR(16)',
        )
