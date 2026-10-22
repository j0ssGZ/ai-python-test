import asyncio
import os
import signal
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from notification_service.config import Settings
from notification_service.domain import FailureCategory, ProcessingError, Status
from notification_service.repository import SCHEMA, SQLiteRepository
from notification_service.service import RequestService
from test_pipeline import AI, Clock, Delivery, activate, pool


@pytest.mark.parametrize("stage", ["extract", "notify"])
async def test_fencing_before_and_after_reclaim(tmp_path, stage):
    path, clock = str(tmp_path / "jobs.db"), Clock()
    async with SQLiteRepository(path) as a, SQLiteRepository(path) as b:
        request_id = await activate(a, Settings(), clock)
        if stage == "notify":
            await pool(a, AI(), Delivery(), clock).run_once("extract")
        first = await a.claim(stage, 100, 30, 3)
        assert await b.claim(stage, 129, 30, 3) is None
        error = ProcessingError(FailureCategory.INFRASTRUCTURE, "late")
        assert not await a.finish(first, 130, error=error)
        second = await b.claim(stage, 130, 30, 3)
        assert first.token != second.token
        assert second.attempt == 2
        assert not await a.finish(first, 131, error=error)
        assert await b.finish(second, 131, error=error)
        assert (await a.get(request_id)).status == Status.FAILED


async def test_one_claim_wins_across_competing_connections(tmp_path):
    path, clock = str(tmp_path / "jobs.db"), Clock()
    async with SQLiteRepository(path) as a, SQLiteRepository(path) as b:
        await activate(a, Settings(), clock)
        results = await asyncio.gather(
            *[(a if i % 2 else b).claim("extract", 100, 30, 3) for i in range(100)]
        )
        assert sum(item is not None for item in results) == 1


@pytest.mark.parametrize("stage", ["extract", "notify"])
async def test_final_abandoned_attempt_becomes_terminal(tmp_path, stage):
    clock = Clock()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        request_id = await activate(repo, Settings(), clock)
        if stage == "notify":
            await pool(repo, AI(), Delivery(), clock).run_once("extract")
        assert await repo.claim(stage, 100, 30, 1)
        assert await repo.claim(stage, 131, 30, 1) is None
        assert (await repo.get(request_id)).status == Status.FAILED
        async with repo.db.execute("SELECT outcome FROM attempts WHERE stage=?", (stage,)) as c:
            assert (await c.fetchone())[0] == "abandoned"


@pytest.mark.parametrize("stage", ["extract", "notify"])
async def test_sigkill_recovery_preserves_stage(tmp_path, stage):
    path, clock = str(tmp_path / "jobs.db"), Clock()
    async with SQLiteRepository(path) as repo:
        request_id = await activate(repo, Settings(), clock)
        if stage == "notify":
            await pool(repo, AI(), Delivery(), clock).run_once("extract")
    script = """
import asyncio, os, signal, sys
from notification_service.repository import SQLiteRepository
async def main():
    async with SQLiteRepository(sys.argv[1]) as repo:
        assert await repo.claim(sys.argv[2], 100, 30, 3)
        os.kill(os.getpid(), signal.SIGKILL)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        path,
        stage,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGKILL
    async with SQLiteRepository(path) as repo:
        assert (await repo.get(request_id)).status == Status.PROCESSING
        recovered = await repo.claim(stage, 131, 30, 3)
        assert recovered.attempt == 2
        assert (recovered.extracted_json is not None) == (stage == "notify")


async def test_cancellation_during_rollback_cannot_poison_connection(tmp_path):
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        entered, release = asyncio.Event(), asyncio.Event()
        rollback = repo.db.rollback

        async def delayed_rollback():
            entered.set()
            await release.wait()
            await rollback()

        async def fail_transaction():
            async with repo.transaction():
                raise RuntimeError("force rollback")

        with patch.object(repo.db, "rollback", delayed_rollback):
            task = asyncio.create_task(fail_transaction())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises((RuntimeError, asyncio.CancelledError)):
                await task
        assert await RequestService(repo, Settings()).create("connection remains usable")


async def test_separate_worker_limits_and_repeated_process_calls(tmp_path):
    clock, entered, release, delivered = Clock(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingAI(AI):
        active = 0
        peak = 0

        async def extract(self, source):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 3:
                entered.set()
            await release.wait()
            self.active -= 1
            return await super().extract(source)

    class RecordingDelivery(Delivery):
        async def send(self, request_id, notification):
            result = await super().send(request_id, notification)
            if len(self.calls) == 8:
                delivered.set()
            return result

    ai, delivery = BlockingAI(), RecordingDelivery()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        ids = [await activate(repo, Settings(), clock) for _ in range(8)]
        await asyncio.gather(*[repo.activate(ids[0], 101) for _ in range(100)])
        workers = pool(repo, ai, delivery, clock, ai_workers=3, notify_workers=2)
        workers.start()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert ai.peak == 3
            release.set()
            await asyncio.wait_for(delivered.wait(), 3)
        finally:
            release.set()
            await workers.stop()
        assert ai.calls == 8
        assert sorted(item[0] for item in delivery.calls) == sorted(ids)
        for request_id in ids:
            assert (await repo.get(request_id)).status == Status.SENT


async def test_shutdown_during_quota_wait_does_not_claim_or_send(tmp_path):
    clock, entered, release = Clock(), asyncio.Event(), asyncio.Event()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        await activate(repo, Settings(), clock)
        delivery = Delivery()
        workers = pool(repo, AI(), delivery, clock)
        await workers.run_once("extract")

        async def blocked_quota():
            entered.set()
            await release.wait()

        with patch.object(workers.limiter, "acquire", blocked_quota):
            task = asyncio.create_task(workers.run_once("notify"))
            await asyncio.wait_for(entered.wait(), 2)
            workers.stopping.set()
            release.set()
            assert not await task
        assert not delivery.calls
        async with repo.db.execute("SELECT count(*) FROM attempts WHERE stage='notify'") as c:
            assert (await c.fetchone())[0] == 0


async def test_forced_shutdown_leaves_recoverable_lease(tmp_path):
    clock, entered = Clock(), asyncio.Event()

    class HangingAI:
        async def extract(self, source):
            entered.set()
            await asyncio.Event().wait()

    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        await activate(repo, Settings(), clock)
        workers = pool(repo, HangingAI(), Delivery(), clock, ai_workers=1, shutdown_seconds=0.01)
        workers.start()
        await asyncio.wait_for(entered.wait(), 2)
        await workers.stop()
        assert all(task.done() for task in workers.tasks)
        assert await repo.claim("extract", 131, 30, 3)


async def test_429_persists_shared_cooldown_even_on_last_attempt(tmp_path):
    clock = Clock()

    class Throttled(Delivery):
        async def send(self, request_id, notification):
            raise ProcessingError(
                FailureCategory.DELIVERY, "notify_http_429", retryable=True, retry_after=20
            )

    path = str(tmp_path / "jobs.db")
    async with SQLiteRepository(path) as repo:
        for _ in range(2):
            await activate(repo, Settings(), clock)
        workers = pool(repo, AI(), Throttled(), clock, notify_max_attempts=1)
        await workers.run_once("extract")
        await workers.run_once("extract")
        await workers.run_once("notify")
    async with SQLiteRepository(path) as repo:
        assert not await repo.has_due("notify", 119)
        assert await repo.has_due("notify", 120)


async def test_schema_upgrade_keeps_existing_request(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO requests VALUES('old','original','queued',1,1,NULL)")
    async with SQLiteRepository(path) as repo:
        assert (await repo.get("old")).user_input == "original"
        assert await repo.claim("extract", 100, 30, 3) is None


async def test_notification_concurrency_is_bounded_independently(tmp_path):
    clock, entered, release, complete = Clock(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingDelivery(Delivery):
        active = 0
        peak = 0

        async def send(self, request_id, notification):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                entered.set()
            await release.wait()
            result = await super().send(request_id, notification)
            self.active -= 1
            if len(self.calls) == 6:
                complete.set()
            return result

    delivery = BlockingDelivery()
    async with SQLiteRepository(str(tmp_path / "jobs.db")) as repo:
        for _ in range(6):
            await activate(repo, Settings(), clock)
        workers = pool(repo, AI(), delivery, clock, ai_workers=3, notify_workers=2)
        workers.start()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert delivery.peak == 2
            release.set()
            await asyncio.wait_for(complete.wait(), 3)
        finally:
            release.set()
            await workers.stop()
        assert delivery.peak == 2


async def test_concurrent_quota_reservations_and_restart_cooldown(tmp_path):
    path, clock = str(tmp_path / "jobs.db"), Clock()
    async with SQLiteRepository(path) as a, SQLiteRepository(path) as b:
        reservations = await asyncio.gather(
            *[(a if i % 2 else b).reserve_rate_slot(100, 50, 10, 0) for i in range(100)]
        )
        assert reservations.count(0) == 50
        workers = pool(a, AI(), Delivery(), clock)
        await workers.limiter.start()
        assert await b.reserve_rate_slot(100, 50, 10, 0) == 15
