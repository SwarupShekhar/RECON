from datetime import datetime

from pydantic import BaseModel


class TrackRequest(BaseModel):
    sender_email: str
    recipient_email: str
    subject: str
    thread_id: str | None = None
    recipient_field: str = "to"


class TrackResponse(BaseModel):
    tracker_id: str


class OpenRecord(BaseModel):
    opened_at: datetime
    verified: bool


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
