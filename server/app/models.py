import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


def gen_uuid():
    return str(uuid.uuid4())


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    sender_email: Mapped[str] = mapped_column(String(255), index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), index=True)
    recipient_field: Mapped[str] = mapped_column(String(10), default="to")
    subject: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    opens: Mapped[list["Open"]] = relationship(back_populates="email", cascade="all, delete-orphan")


class Open(Base):
    __tablename__ = "opens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(String(36), ForeignKey("emails.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)

    email: Mapped["Email"] = relationship(back_populates="opens")
