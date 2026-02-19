from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NotificationSettings(Base):
    __tablename__ = 'notification_settings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    smtp_host: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587, nullable=False)
    smtp_user: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    smtp_password: Mapped[str] = mapped_column(Text, default='', nullable=False)
    smtp_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    from_email: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    to_emails_csv: Mapped[str] = mapped_column(Text, default='', nullable=False)
    notify_on_job_complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_job_failed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_job_interrupted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_low_disk_pause: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_recovery_ran: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_on_batch_complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
