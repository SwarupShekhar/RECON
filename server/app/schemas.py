from datetime import datetime

from pydantic import BaseModel


class LinkIn(BaseModel):
    id: str | None = None
    url: str
    type: str = "link"


class LinkOut(BaseModel):
    link_id: str
    tracked_url: str


class RecipientIn(BaseModel):
    email: str
    field: str = "to"


class TrackRequest(BaseModel):
    id: str | None = None
    sender_email: str
    recipient_email: str
    subject: str
    thread_id: str | None = None
    recipient_field: str = "to"
    links: list[LinkIn] = []
    all_recipients: list[RecipientIn] = []


class TrackResponse(BaseModel):
    tracker_id: str
    links: list[LinkOut] = []


class MuteRequest(BaseModel):
    thread_id: str | None = None
    email_ids: list[str] = []
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
