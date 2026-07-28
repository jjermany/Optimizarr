from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SabnzbdSettings(Base):
    __tablename__ = 'sabnzbd_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    host: Mapped[str] = mapped_column(String(255), default='http://localhost', nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=8080, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, default='', nullable=False)
    max_download_retries: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
