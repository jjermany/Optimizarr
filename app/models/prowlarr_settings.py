from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProwlarrSettings(Base):
    __tablename__ = 'prowlarr_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    host: Mapped[str] = mapped_column(String(255), default='http://localhost:9696', nullable=False)
    api_key: Mapped[str] = mapped_column(Text, default='', nullable=False)
