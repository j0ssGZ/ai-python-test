import asyncio
import time
from collections.abc import Awaitable, Callable

from notification_service.config import Settings
from notification_service.repository import SQLiteRepository


class RateLimiter:
    def __init__(
        self,
        repository: SQLiteRepository,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.repository, self.settings = repository, settings
        self.clock, self.sleep = clock, sleep

    async def start(self) -> None:
        # Conservatively cover previous in-flight attempts after a process restart.
        await self.repository.cooldown(
            "notify", self.clock() + self.settings.notify_rate_window + self.settings.notify_timeout
        )

    async def acquire(self) -> None:
        while True:
            delay = await self.repository.reserve_rate_slot(
                self.clock(),
                self.settings.notify_rate_limit,
                self.settings.notify_rate_window,
                self.settings.notify_spacing,
            )
            if delay == 0:
                return
            await self.sleep(delay)
