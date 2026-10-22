from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class FailureCategory(StrEnum):
    EXTRACTION = "extraction"
    SEMANTIC = "semantic"
    REFUSAL = "refusal"
    INFRASTRUCTURE = "infrastructure"
    DELIVERY = "delivery"


class NotFound(Exception):
    pass


class BacklogFull(Exception):
    pass


class InvalidInput(Exception):
    pass


@dataclass(frozen=True)
class RequestRecord:
    id: str
    user_input: str
    status: Status
    created_at: float
    activated_at: float | None = None
