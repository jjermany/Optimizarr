from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlexSettings(Base):
    __tablename__ = 'plex_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    host: Mapped[str] = mapped_column(String(255), default='http://localhost', nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=32400, nullable=False)
    token: Mapped[str] = mapped_column(Text, default='', nullable=False)
