from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Settings(Base):
    __tablename__ = 'settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enable_optimizer: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    target_resolution: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    bitrate_mbps: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    keep_original: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    schedule_start_hour: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    schedule_end_hour: Mapped[int] = mapped_column(Integer, default=23, nullable=False)
    process_hdr_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
