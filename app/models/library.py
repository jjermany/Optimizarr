from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CodecEnum(str, Enum):
    h264 = 'h264'
    hevc = 'hevc'
    av1 = 'av1'


class ContainerEnum(str, Enum):
    mkv = 'mkv'
    mp4 = 'mp4'


class AudioModeEnum(str, Enum):
    copy = 'copy'
    aac = 'aac'
    ac3 = 'ac3'
    eac3 = 'eac3'


class BitrateModeEnum(str, Enum):
    cbr = 'cbr'
    vbr_crf = 'vbr_crf'


class SpeedPresetEnum(str, Enum):
    slow = 'slow'
    medium = 'medium'
    fast = 'fast'


class SchedulePolicyEnum(str, Enum):
    finish_current = 'finish_current'
    pause_current = 'pause_current'


class OutputConflictPolicyEnum(str, Enum):
    skip = 'skip'
    overwrite = 'overwrite'
    rename = 'rename'


class DownloadQualityProfileEnum(str, Enum):
    any = 'any'
    remux = 'remux'
    web_dl = 'web_dl'
    webrip = 'webrip'
    bluray = 'bluray'
    hdtv = 'hdtv'


class PreferredEncoderEnum(str, Enum):
    auto = 'auto'
    # QSV (Intel oneVPL — requires VPL GPU runtime in container)
    h264_qsv = 'h264_qsv'
    hevc_qsv = 'hevc_qsv'
    av1_qsv = 'av1_qsv'
    # VAAPI (Intel iHD driver — same path as Plex, no VPL runtime needed)
    h264_vaapi = 'h264_vaapi'
    hevc_vaapi = 'hevc_vaapi'
    av1_vaapi = 'av1_vaapi'
    # Software
    libx264 = 'libx264'
    libx265 = 'libx265'
    libsvtav1 = 'libsvtav1'


class Library(Base):
    __tablename__ = 'libraries'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    profile: Mapped['LibraryProfile | None'] = relationship(
        'LibraryProfile',
        back_populates='library',
        uselist=False,
        cascade='all, delete-orphan',
    )


class LibraryProfile(Base):
    __tablename__ = 'library_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    library_id: Mapped[int] = mapped_column(ForeignKey('libraries.id'), unique=True, nullable=False)
    target_resolution: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    minimum_source_resolution: Mapped[int] = mapped_column(Integer, default=2160, nullable=False)
    codec: Mapped[CodecEnum] = mapped_column(SqlEnum(CodecEnum), default=CodecEnum.hevc, nullable=False)
    container: Mapped[ContainerEnum] = mapped_column(SqlEnum(ContainerEnum), default=ContainerEnum.mkv, nullable=False)
    audio_mode: Mapped[AudioModeEnum] = mapped_column(SqlEnum(AudioModeEnum), default=AudioModeEnum.copy, nullable=False)
    bitrate_mode: Mapped[BitrateModeEnum] = mapped_column(SqlEnum(BitrateModeEnum), default=BitrateModeEnum.vbr_crf, nullable=False)
    bitrate_mbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crf: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_preset: Mapped[SpeedPresetEnum] = mapped_column(SqlEnum(SpeedPresetEnum), default=SpeedPresetEnum.medium, nullable=False)
    hdr_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tone_map_hdr: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_start_hour: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schedule_end_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    schedule_policy: Mapped[SchedulePolicyEnum] = mapped_column(
        SqlEnum(SchedulePolicyEnum),
        default=SchedulePolicyEnum.finish_current,
        nullable=False,
    )
    output_suffix: Mapped[str] = mapped_column(String(64), default='-1080p', nullable=False)
    output_conflict_policy: Mapped[OutputConflictPolicyEnum] = mapped_column(
        SqlEnum(OutputConflictPolicyEnum),
        default=OutputConflictPolicyEnum.skip,
        nullable=False,
    )
    av1_fallback_codec: Mapped[CodecEnum] = mapped_column(SqlEnum(CodecEnum), default=CodecEnum.hevc, nullable=False)
    preferred_video_encoder: Mapped[PreferredEncoderEnum] = mapped_column(
        SqlEnum(PreferredEncoderEnum),
        default=PreferredEncoderEnum.auto,
        nullable=False,
    )
    plex_library_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    download_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    download_timeout_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    download_codec: Mapped[CodecEnum | None] = mapped_column(SqlEnum(CodecEnum), nullable=True)
    download_fallback_codec: Mapped[CodecEnum | None] = mapped_column(SqlEnum(CodecEnum), nullable=True)
    download_quality_profile: Mapped[DownloadQualityProfileEnum] = mapped_column(
        SqlEnum(DownloadQualityProfileEnum),
        default=DownloadQualityProfileEnum.any,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    library: Mapped[Library] = relationship('Library', back_populates='profile')
