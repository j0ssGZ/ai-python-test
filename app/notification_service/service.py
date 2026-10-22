import time
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from notification_service.config import Settings
from notification_service.domain import InvalidInput, RequestRecord, Status


class RequestRepository(Protocol):
    async def create(self, record: RequestRecord, capacity: int) -> None: ...
    async def get(self, request_id: str) -> RequestRecord: ...
    async def activate(self, request_id: str, now: float) -> RequestRecord: ...


class RequestService:
    def __init__(
        self,
        repository: RequestRepository,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ):
        self.repository = repository
        self.settings = settings
        self.clock = clock

    async def create(self, user_input: str) -> RequestRecord:
        if not user_input.strip() or len(user_input) > self.settings.max_input_chars:
            raise InvalidInput
        record = RequestRecord(str(uuid4()), user_input, Status.QUEUED, self.clock())
        await self.repository.create(record, self.settings.backlog_capacity)
        return record

    async def activate(self, request_id: str) -> RequestRecord:
        return await self.repository.activate(request_id, self.clock())

    async def get(self, request_id: str) -> RequestRecord:
        return await self.repository.get(request_id)
