from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DiscoveryFileIndex(Base):
    __tablename__ = 'discovery_file_index'
    __table_args__ = (
        UniqueConstraint('library_id', 'source_path', name='uq_discovery_file_index_library_source'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    library_id: Mapped[int] = mapped_column(ForeignKey('libraries.id'), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discovery_signature: Mapped[str] = mapped_column(String(255), nullable=False, default='')
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
