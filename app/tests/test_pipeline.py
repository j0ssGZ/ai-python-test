import asyncio
import json

import httpx
import pytest
from notification_service.config import Settings
from notification_service.delivery_client import DeliveryClient
from notification_service.domain import FailureCategory, ProcessingError, Status
from notification_service.extraction import Notification, ParsedExtraction
from notification_service.rate_limiter import RateLimiter
from notification_service.repository import SQLiteRepository
from notification_service.service import RequestService
from notification_service.workers import WorkerPool


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


class AI:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    async def extract(self, source):
        self.calls += 1
        if self.failure:
            raise self.failure
        return ParsedExtraction(
            Notification(to="test@example.org", message="ready", type="email"), False
        )


class Delivery:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    async def send(self, request_id, notification):
        self.calls.append((request_id, notification))
        if len(self.calls) <= self.failures:
            raise ProcessingError(FailureCategory.DELIVERY, "notify_http_503", retryable=True)
        return "p-confirmed"


async def activate(repo, settings, clock):
    service = RequestService(repo, settings, clock)
    record = await service.create("Send email to test@example.org: ready")
    await service.activate(record.id)
    return record.id


def pool(repo, ai, delivery, clock, **kwargs):
    settings = Settings(**kwargs)
    limiter = RateLimiter(repo, settings, clock=clock, sleep=clock.sleep)
    return WorkerPool(repo, ai, delivery, limiter, settings, clock=clock, random=lambda: 1)


async def test_durable_stage_boundary_and_delivery_retry_never_repeat_ai(tmp_path):
    path = str(tmp_path / "jobs.db")
    clock, ai, delivery = Clock(), AI(), Delivery(failures=1)
    async with SQLiteRepository(path) as repo:
        request_id = await activate(repo, Settings(), clock)
        workers = pool(repo, ai, delivery, clock)
        assert await workers.run_once("extract")
        assert ai.calls == 1
        assert delivery.calls == []
    async with SQLiteRepository(path) as repo:
        workers = pool(repo, ai, delivery, clock)
        assert not await workers.run_once("extract")
        assert await workers.run_once("notify")
        assert (await repo.get(request_id)).status == Status.PROCESSING
        clock.now += 2
        assert await workers.run_once("notify")
        assert (await repo.get(request_id)).status == Status.SENT
        assert ai.calls == 1
        assert len(delivery.calls) == 2
        assert delivery.calls[0] == delivery.calls[1]


@pytest.mark.parametrize("category", list(FailureCategory))
async def test_terminal_errors_remain_distinguishable_and_never_send(tmp_path, category):
    clock, delivery = Clock(), Delivery()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        request_id = await activate(repo, Settings(), clock)
        workers = pool(repo, AI(ProcessingError(category, "test_failure")), delivery, clock)
        await workers.run_once("extract")
        assert (await repo.get(request_id)).status == Status.FAILED
        assert not await workers.run_once("notify")
        assert not delivery.calls
        async with repo.db.execute(
            "SELECT failure_category FROM requests WHERE id=?", (request_id,)
        ) as c:
            assert (await c.fetchone())[0] == category.value


async def test_attempt_budget_and_backoff_survive_reopen(tmp_path):
    path, clock = str(tmp_path / "jobs.db"), Clock()
    ai = AI(ProcessingError(FailureCategory.INFRASTRUCTURE, "timeout", retryable=True))
    async with SQLiteRepository(path) as repo:
        request_id = await activate(repo, Settings(), clock)
        await pool(repo, ai, Delivery(), clock, ai_max_attempts=2).run_once("extract")
    async with SQLiteRepository(path) as repo:
        workers = pool(repo, ai, Delivery(), clock, ai_max_attempts=2)
        assert not await workers.run_once("extract")
        clock.now += 1
        assert await workers.run_once("extract")
        assert (await repo.get(request_id)).status == Status.FAILED
        assert ai.calls == 2


async def test_unactivated_requests_never_run(tmp_path):
    clock, ai, delivery = Clock(), AI(), Delivery()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        await RequestService(repo, Settings(), clock).create(
            "Send email to test@example.org: ready"
        )
        workers = pool(repo, ai, delivery, clock)
        assert not await workers.run_once("extract")
        assert not await workers.run_once("notify")
        assert ai.calls == 0


async def test_rate_limit_is_durable_and_bounds_every_sliding_window(tmp_path):
    path, clock, admitted = str(tmp_path / "jobs.db"), Clock(), []
    settings = Settings(notify_spacing=0)
    for _ in range(2):
        async with SQLiteRepository(path) as repo:
            limiter = RateLimiter(repo, settings, clock=clock, sleep=clock.sleep)
            for _ in range(75):
                await limiter.acquire()
                admitted.append(clock())
    for now in admitted:
        assert sum(now - 10 < item <= now for item in admitted) <= 50


async def test_notify_client_validates_confirmation_and_correlates_request():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1234"})

    notification = Notification(to="test@example.org", message="ready", type="email")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://provider"
    ) as c:
        assert await DeliveryClient(c, "key").send("request-id", notification) == "p-1234"
    assert calls[0].url.path == "/v1/notify"
    assert calls[0].url.params["trace_id"] == "request-id"
    assert json.loads(calls[0].content) == notification.model_dump()
    assert calls[0].headers["X-API-Key"] == "key"


@pytest.mark.parametrize(
    "code,body,retry",
    [
        (200, {}, False),
        (200, {"status": "delivered", "provider_id": " "}, False),
        (200, {"status": "queued", "provider_id": "p-1234"}, False),
        (429, {}, True),
        (500, {}, True),
        (401, {}, False),
        (302, {}, False),
    ],
)
async def test_notify_errors(code, body, retry):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(code, json=body)),
        base_url="http://provider",
    ) as c:
        with pytest.raises(ProcessingError) as caught:
            await DeliveryClient(c, "key").send(
                "id", Notification(to="test@example.org", message="ready", type="email")
            )
    assert caught.value.retryable == retry


async def test_timeout_is_bounded_and_classified(tmp_path):
    class HangingAI:
        async def extract(self, source):
            await asyncio.Event().wait()

    clock = Clock()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        request_id = await activate(repo, Settings(), clock)
        workers = pool(repo, HangingAI(), Delivery(), clock, ai_timeout=0.01, ai_max_attempts=1)
        await workers.run_once("extract")
        assert (await repo.get(request_id)).status == Status.FAILED
