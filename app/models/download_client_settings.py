from enum import Enum

from sqlalchemy import Boolean, Enum as SqlEnum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DownloadClientTypeEnum(str, Enum):
    qbittorrent = 'qbittorrent'
    sabnzbd = 'sabnzbd'


class DownloadClientSettings(Base):
    __tablename__ = 'download_client_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    client_type: Mapped[DownloadClientTypeEnum] = mapped_column(
        SqlEnum(DownloadClientTypeEnum),
        default=DownloadClientTypeEnum.qbittorrent,
        nullable=False,
    )
    host: Mapped[str] = mapped_column(String(255), default='http://localhost', nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=8080, nullable=False)
    username: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    password: Mapped[str] = mapped_column(Text, default='', nullable=False)
    api_key: Mapped[str] = mapped_column(Text, default='', nullable=False)
