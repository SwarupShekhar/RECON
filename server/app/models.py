import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


def gen_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Clerk user id
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    slack_webhook_url: Mapped[str | None] = mapped_column(Text, default=None)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="api_keys")


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    sender_email: Mapped[str] = mapped_column(String(255), index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), index=True)
    recipient_field: Mapped[str] = mapped_column(String(10), default="to")
    subject: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    all_recipients: Mapped[str | None] = mapped_column(Text, default=None)
    user_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("users.id"), index=True, default=None)

    opens: Mapped[list["Open"]] = relationship(back_populates="email", cascade="all, delete-orphan")


class Open(Base):
    __tablename__ = "opens"
    __table_args__ = (
        # Speeds up dashboard/status aggregation which always filters
        # opens by email_id together with internal (human vs. proxy).
        Index("ix_opens_email_internal", "email_id", "internal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(String(36), ForeignKey("emails.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)

    email: Mapped["Email"] = relationship(back_populates="opens")


class PixelMute(Base):
    __tablename__ = "pixel_mutes"

    thread_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    muted_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailMute(Base):
    __tablename__ = "email_mutes"

    email_id: Mapped[str] = mapped_column(String(36), ForeignKey("emails.id"), primary_key=True)
    muted_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Link(Base):
    __tablename__ = "links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    email_id: Mapped[str] = mapped_column(String(36), ForeignKey("emails.id"), index=True)
    original_url: Mapped[str] = mapped_column(Text)
    link_type: Mapped[str] = mapped_column(String(10), default="link")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    clicks: Mapped[list["LinkClick"]] = relationship(back_populates="link", cascade="all, delete-orphan")


class LinkClick(Base):
    __tablename__ = "link_clicks"
    __table_args__ = (
        # Speeds up dashboard/status aggregation which always filters
        # clicks by link_id together with internal (human vs. proxy).
        Index("ix_link_clicks_link_internal", "link_id", "internal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[str] = mapped_column(String(36), ForeignKey("links.id"), index=True)
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)

    link: Mapped["Link"] = relationship(back_populates="clicks")
