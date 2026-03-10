from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Job(Base):
    __tablename__ = 'jobs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    input_path: Mapped[str] = mapped_column(Text, nullable=False)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default='queued', nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    library_id: Mapped[int | None] = mapped_column(ForeignKey('libraries.id'), nullable=True)
    profile_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    encoder_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    codec_used: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hwaccel_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    used_fallback: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_resolution: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_is_hdr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resume_position_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    encode_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_encode_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    encode_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def source_path(self) -> str:
        return self.input_path

    @source_path.setter
    def source_path(self, value: str) -> None:
        self.input_path = value


OptimizationJob = Job
