from sqlalchemy import create_engine, text

from app.core import database


def test_auto_migration_persists_long_preferred_video_encoder_values(tmp_path):
    db_path = tmp_path / 'legacy.db'
    temp_engine = create_engine(f'sqlite:///{db_path}', connect_args={'check_same_thread': False})

    with temp_engine.begin() as connection:
        connection.execute(text('''
            CREATE TABLE library_profiles (
                id INTEGER NOT NULL PRIMARY KEY,
                library_id INTEGER NOT NULL UNIQUE,
                target_resolution INTEGER NOT NULL DEFAULT 1080,
                minimum_source_resolution INTEGER NOT NULL DEFAULT 2160,
                codec VARCHAR(4) NOT NULL DEFAULT 'hevc',
                container VARCHAR(3) NOT NULL DEFAULT 'mkv',
                audio_mode VARCHAR(4) NOT NULL DEFAULT 'copy',
                bitrate_mode VARCHAR(7) NOT NULL DEFAULT 'vbr_crf',
                bitrate_mbps INTEGER,
                crf INTEGER,
                speed_preset VARCHAR(6) NOT NULL DEFAULT 'medium',
                hdr_only BOOLEAN NOT NULL DEFAULT 1,
                tone_map_hdr BOOLEAN NOT NULL DEFAULT 0,
                max_workers INTEGER NOT NULL DEFAULT 1,
                schedule_enabled BOOLEAN NOT NULL DEFAULT 1,
                schedule_start_hour INTEGER NOT NULL DEFAULT 1,
                schedule_end_hour INTEGER NOT NULL DEFAULT 9,
                schedule_policy VARCHAR(14) NOT NULL DEFAULT 'finish_current',
                output_suffix VARCHAR(64) NOT NULL DEFAULT '-1080p',
                output_conflict_policy VARCHAR(9) NOT NULL DEFAULT 'skip',
                av1_fallback_codec VARCHAR(4) NOT NULL DEFAULT 'hevc',
                plex_library_id VARCHAR(16),
                download_enabled BOOLEAN NOT NULL DEFAULT 0,
                download_timeout_minutes INTEGER NOT NULL DEFAULT 60,
                download_codec VARCHAR(4),
                download_fallback_codec VARCHAR(4),
                download_quality_profile VARCHAR(6) NOT NULL DEFAULT 'any',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        connection.execute(text('''
            CREATE TABLE sabnzbd_settings (
                id INTEGER NOT NULL PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                host VARCHAR(255) NOT NULL DEFAULT 'http://localhost',
                port INTEGER NOT NULL DEFAULT 8080,
                api_key TEXT NOT NULL DEFAULT ''
            )
        '''))
        connection.execute(text('''
            CREATE TABLE qbittorrent_settings (
                id INTEGER NOT NULL PRIMARY KEY,
                enabled BOOLEAN NOT NULL DEFAULT 0,
                host VARCHAR(255) NOT NULL DEFAULT 'http://localhost',
                port INTEGER NOT NULL DEFAULT 8080,
                username VARCHAR(255) NOT NULL DEFAULT 'admin',
                password TEXT NOT NULL DEFAULT ''
            )
        '''))

    original_engine = database.engine
    original_bind = database.SessionLocal.kw.get('bind')
    try:
        database.engine = temp_engine
        database.SessionLocal.configure(bind=temp_engine)
        database.init_db()

        with temp_engine.begin() as connection:
            preferred_column = next(
                row for row in connection.execute(text('PRAGMA table_info(library_profiles)'))
                if row[1] == 'preferred_video_encoder'
            )
            assert preferred_column[2] == 'VARCHAR(32)'
            sab_retry_column = next(
                row for row in connection.execute(text('PRAGMA table_info(sabnzbd_settings)'))
                if row[1] == 'max_download_retries'
            )
            assert sab_retry_column[2] == 'INTEGER'
            assert sab_retry_column[4] == '10'
            qbt_retry_column = next(
                row for row in connection.execute(text('PRAGMA table_info(qbittorrent_settings)'))
                if row[1] == 'max_download_retries'
            )
            assert qbt_retry_column[2] == 'INTEGER'
            assert qbt_retry_column[4] == '1'

            connection.execute(text(
                "INSERT INTO libraries (id, name, path, enabled, created_at, updated_at) "
                "VALUES (1, 'Movies', '/media/movies', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            for profile_id, encoder in enumerate(('hevc_vaapi', 'libsvtav1'), start=1):
                connection.execute(
                    text(
                        'INSERT INTO library_profiles (id, library_id, preferred_video_encoder) '
                        'VALUES (:id, :library_id, :encoder)'
                    ),
                    {'id': profile_id, 'library_id': profile_id, 'encoder': encoder},
                )
                if profile_id == 1:
                    connection.execute(text(
                        "INSERT INTO libraries (id, name, path, enabled, created_at, updated_at) "
                        "VALUES (2, 'TV', '/media/tv', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ))

            persisted = connection.execute(text(
                'SELECT preferred_video_encoder FROM library_profiles ORDER BY id'
            )).scalars().all()

        assert persisted == ['hevc_vaapi', 'libsvtav1']
    finally:
        database.SessionLocal.configure(bind=original_bind)
        database.engine = original_engine
        temp_engine.dispose()
