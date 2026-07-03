import os
from enum import Enum

from sqlalchemy import Boolean, Enum as SqlEnum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiscoveryMethodEnum(str, Enum):
    interval = 'interval'
    watcher = 'watcher'


class QueueSortEnum(str, Enum):
    default = 'default'
    newest = 'newest'
    oldest = 'oldest'
    year_newest = 'year_newest'
    year_oldest = 'year_oldest'


def clamp_scan_probe_workers(value: int | None) -> int:
    cpu_count = max(1, os.cpu_count() or 1)
    requested = 1 if value is None else int(value)
    return max(1, min(requested, cpu_count))


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
    global_quiet_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    global_quiet_start_hour: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    global_quiet_end_hour: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    process_hdr_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    history_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    auto_discovery_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    discovery_method: Mapped[DiscoveryMethodEnum] = mapped_column(
        SqlEnum(DiscoveryMethodEnum),
        default=DiscoveryMethodEnum.interval,
        nullable=False,
    )
    discovery_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    queue_sort: Mapped[QueueSortEnum] = mapped_column(
        SqlEnum(QueueSortEnum),
        default=QueueSortEnum.default,
        nullable=False,
    )
    workspace_root: Mapped[str] = mapped_column(String(512), default='/cache/workspaces', nullable=False)
    scan_probe_workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    min_free_gb: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    requeue_interrupted_jobs: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cleanup_workspaces_on_startup: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    duplicate_cleanup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duplicate_cleanup_interval_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    queue_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    qbt_strike_check_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    qbt_metadata_max_strikes: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    qbt_stalled_max_strikes: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    qbt_slow_min_speed_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qbt_slow_max_strikes: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    qbt_slow_ignore_private: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
