import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Protocol

from notification_service.config import Settings
from notification_service.domain import FailureCategory, ProcessingError
from notification_service.extraction import Notification, ParsedExtraction
from notification_service.rate_limiter import RateLimiter
from notification_service.repository import SQLiteRepository

logger = logging.getLogger(__name__)


class Extractor(Protocol):
    async def extract(self, user_input: str) -> ParsedExtraction: ...


class Sender(Protocol):
    async def send(self, request_id: str, notification: Notification) -> str: ...


class WorkerPool:
    def __init__(
        self,
        repository: SQLiteRepository,
        ai: Extractor,
        delivery: Sender,
        limiter: RateLimiter,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        random: Callable[[], float] = random.random,
    ):
        self.repository, self.ai, self.delivery = repository, ai, delivery
        self.limiter, self.settings = limiter, settings
        self.clock, self.random = clock, random
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    async def run_once(self, stage: str) -> bool:
        if self.stopping.is_set() or not await self.repository.has_due(stage, self.clock()):
            return False
        if stage == "notify":
            await self.limiter.acquire()
        if self.stopping.is_set():
            return False
        extracting = stage == "extract"
        attempts = (
            self.settings.ai_max_attempts if extracting else self.settings.notify_max_attempts
        )
        timeout = self.settings.ai_timeout if extracting else self.settings.notify_timeout
        item = await self.repository.claim(
            stage, self.clock(), self.settings.lease_seconds, attempts
        )
        if item is None:
            return False
        extraction, provider_id = None, None
        try:
            async with asyncio.timeout(timeout):
                if extracting:
                    extraction = await self.ai.extract(item.user_input)
                else:
                    notification = Notification.model_validate_json(item.extracted_json)
                    provider_id = await self.delivery.send(item.id, notification)
        except (ProcessingError, TimeoutError) as exc:
            error = (
                exc
                if isinstance(exc, ProcessingError)
                else ProcessingError(
                    FailureCategory.INFRASTRUCTURE, "attempt_timeout", retryable=True
                )
            )
            next_at = None
            delay = self.random() * min(
                self.settings.retry_cap, self.settings.retry_base * 2 ** (item.attempt - 1)
            )
            if error.retry_after is not None:
                delay = max(delay, error.retry_after)
            if error.code.endswith("_429"):
                delay = max(delay, self.settings.notify_rate_window)
                await self.repository.cooldown(stage, self.clock() + delay)
            if error.retryable and item.attempt < attempts:
                next_at = self.clock() + delay
            saved = await self.repository.finish(
                item, self.clock(), error=error, next_attempt_at=next_at
            )
            logger.info(
                json.dumps(
                    {
                        "event": "attempt_failed",
                        "request_id": item.id,
                        "stage": stage,
                        "attempt": item.attempt,
                        "category": error.category,
                        "code": error.code,
                        "state_saved": saved,
                    }
                )
            )
        else:
            saved = await self.repository.finish(
                item, self.clock(), extraction=extraction, provider_id=provider_id
            )
            logger.info(
                json.dumps(
                    {
                        "event": "attempt_succeeded",
                        "request_id": item.id,
                        "stage": stage,
                        "attempt": item.attempt,
                        "state_saved": saved,
                    }
                )
            )
        return True

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._run(stage), name=f"{stage}-{i}")
            for stage, count in (
                ("extract", self.settings.ai_workers),
                ("notify", self.settings.notify_workers),
            )
            for i in range(count)
        ]

    @property
    def healthy(self) -> bool:
        return bool(self.tasks) and all(not task.done() for task in self.tasks)

    async def _run(self, stage: str) -> None:
        while not self.stopping.is_set():
            try:
                if await self.run_once(stage):
                    continue
            except Exception:
                # Do not log arbitrary external exception text or payloads.
                logger.error(json.dumps({"event": "worker_exception", "stage": stage}))
            try:
                await asyncio.wait_for(self.stopping.wait(), self.settings.poll_interval)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self.stopping.set()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.settings.shutdown_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
