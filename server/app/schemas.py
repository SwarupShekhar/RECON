from datetime import datetime

from pydantic import BaseModel


class LinkIn(BaseModel):
    url: str
    type: str = "link"


class LinkOut(BaseModel):
    link_id: str
    tracked_url: str


class TrackRequest(BaseModel):
    sender_email: str
    recipient_email: str
    subject: str
    thread_id: str | None = None
    recipient_field: str = "to"
    links: list[LinkIn] = []


class TrackResponse(BaseModel):
    tracker_id: str
    links: list[LinkOut] = []


class MuteRequest(BaseModel):
    thread_id: str
    seconds: int = 30


class OpenRecord(BaseModel):
    opened_at: datetime
    verified: bool
    internal: bool = False


class ThreadStatus(BaseModel):
    thread_id: str
    email_id: str
    recipient_email: str
    recipient_field: str
    subject: str
    total_opens: int
    verified_opens: int
    last_opened_at: datetime | None
    opens: list[OpenRecord]


class StatusResponse(BaseModel):
    threads: list[ThreadStatus]


class EmailSummary(BaseModel):
    id: str
    sender: str
    recipient: str
    recipient_field: str
    subject: str
    thread_id: str | None
    created_at: str
    total_opens: int
    verified_opens: int
    last_opened_at: str | None
