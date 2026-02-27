from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DownloadJobStatus(str, Enum):
    pending = 'pending'           # waiting in queue for its turn
    searching = 'searching'
    downloading = 'downloading'
    stalled = 'stalled'
    importing = 'importing'
    complete = 'complete'
    failed = 'failed'
    timed_out = 'timed_out'
    fallback_queued = 'fallback_queued'


class DownloadJob(Base):
    __tablename__ = 'download_jobs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey('libraries.id'), nullable=True)
    source_file_path: Mapped[str] = mapped_column(Text, nullable=False)
    search_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    client_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'qbittorrent' or 'sabnzbd'
    status: Mapped[str] = mapped_column(String(32), default='pending', nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloaded_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    encode_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    download_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
