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


class ProcessingError(Exception):
    def __init__(
        self,
        category: FailureCategory,
        code: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(code)
        self.category = category
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class RequestRecord:
    id: str
    user_input: str
    status: Status
    created_at: float
    activated_at: float | None = None
