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


def init_db() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        settings_columns = {
            row[1] for row in connection.execute(text('PRAGMA table_info(settings)')).fetchall()
        }
        if 'history_retention_days' not in settings_columns:
            connection.execute(
                text('ALTER TABLE settings ADD COLUMN history_retention_days INTEGER NOT NULL DEFAULT 30')
            )
