import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
from notification_service.api import create_app
from notification_service.config import Settings
from notification_service.domain import BacklogFull, FailureCategory, NotFound, Status
from notification_service.repository import SQLiteRepository
from notification_service.service import RequestService
from pydantic import ValidationError


@asynccontextmanager
async def client_at(path, **kwargs):
    app = create_app(Settings(database_path=str(path), **kwargs))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app


async def test_contract_activation_and_restart(tmp_path):
    path = tmp_path / "requests.sqlite3"
    async with client_at(path) as (client, _):
        created = await client.post("/v1/requests", json={"user_input": "Send a message later"})
        assert created.status_code == 201
        assert set(created.json()) == {"id"}
        request_id = created.json()["id"]
        expected = {"id": request_id, "status": "queued"}
        assert (await client.get(f"/v1/requests/{request_id}")).json() == expected
        assert (await client.post(f"/v1/requests/{request_id}/process")).status_code == 202
    async with client_at(path) as (client, app):
        assert (await client.get(f"/v1/requests/{request_id}")).json() == expected
        record = await app.state.repository.get(request_id)
        assert record.user_input == "Send a message later"
        assert record.activated_at is not None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_input": "  "},
        {"user_input": 42},
        {"user_input": None},
        {"user_input": []},
        {"user_input": "a", "to": "injected@example.com"},
    ],
)
async def test_invalid_creation(payload, tmp_path):
    async with client_at(tmp_path / "test.db") as (client, _):
        assert (await client.post("/v1/requests", json=payload)).status_code == 422


async def test_size_limits_and_unknown_ids(tmp_path):
    async with client_at(tmp_path / "test.db", max_input_chars=4, max_body_bytes=100) as (c, _):
        assert (await c.post("/v1/requests", json={"user_input": "abcde"})).status_code == 422
        assert (await c.post("/v1/requests", content=b"x" * 101)).status_code == 413
        assert (await c.get("/v1/requests/unknown")).status_code == 404
        assert (await c.post("/v1/requests/unknown/process")).status_code == 404


async def test_streamed_body_is_bounded_without_content_length(tmp_path):
    async def chunks():
        yield b'{"user_input":"'
        yield b"x" * 200
        yield b'"}'

    async with client_at(tmp_path / "test.db", max_body_bytes=100) as (c, _):
        response = await c.post("/v1/requests", content=chunks())
        assert response.status_code == 413


async def test_repeated_activation_is_idempotent_and_creation_is_independent(tmp_path):
    async with SQLiteRepository(str(tmp_path / "test.db")) as repo:
        service = RequestService(repo, Settings(), clock=lambda: 100)
        first = await service.create("identical text")
        second = await service.create("identical text")
        assert first.id != second.id
        assert first.activated_at is None
        results = await asyncio.gather(*[service.activate(first.id) for _ in range(100)])
        assert all(r.activated_at == 100 and r.status == Status.QUEUED for r in results)
        assert (await repo.get(second.id)).activated_at is None
        with pytest.raises(NotFound):
            await repo.activate("missing", 100)


async def test_admission_is_atomic_across_connections(tmp_path):
    path = str(tmp_path / "test.db")
    async with SQLiteRepository(path) as a, SQLiteRepository(path) as b:
        settings = Settings(backlog_capacity=3)
        services = [RequestService(repo, settings) for repo in (a, b)]
        results = await asyncio.gather(
            *[services[i % 2].create("hello") for i in range(20)], return_exceptions=True
        )
        assert sum(not isinstance(result, Exception) for result in results) == 3
        assert sum(isinstance(result, BacklogFull) for result in results) == 17


async def test_capacity_and_storage_failure_never_acknowledge_work(tmp_path):
    async with client_at(tmp_path / "test.db", backlog_capacity=1) as (c, app):
        assert (await c.post("/v1/requests", json={"user_input": "one"})).status_code == 201
        assert (await c.post("/v1/requests", json={"user_input": "two"})).status_code == 503
        import sqlite3

        with patch.object(app.state.repository, "create", side_effect=sqlite3.OperationalError):
            assert (await c.post("/v1/requests", json={"user_input": "x"})).status_code == 503


async def test_cancelled_transaction_rolls_back_and_connection_remains_usable(tmp_path):
    async with SQLiteRepository(str(tmp_path / "test.db")) as repo:
        started = asyncio.Event()
        original = repo.db.execute

        async def execute(sql, *args):
            result = await original(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                started.set()
                await asyncio.Event().wait()
            return result

        service = RequestService(repo, Settings())
        with patch.object(repo.db, "execute", execute):
            task = asyncio.create_task(service.create("cancelled"))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (await service.create("after cancellation")).user_input == "after cancellation"


def test_settings_and_failure_categories(monkeypatch):
    monkeypatch.setenv("AI_NOTIFY_BACKLOG_CAPACITY", "12")
    assert Settings.from_env().backlog_capacity == 12
    with pytest.raises(ValidationError):
        Settings(backlog_capacity=0)
    assert {v.value for v in FailureCategory} == {
        "extraction",
        "semantic",
        "refusal",
        "infrastructure",
        "delivery",
    }


@pytest.mark.parametrize("status", ["sent", "failed"])
async def test_terminal_activation_is_a_noop(tmp_path, status):
    async with client_at(tmp_path / "test.db") as (client, app):
        created = await client.post("/v1/requests", json={"user_input": "  original text  "})
        request_id = created.json()["id"]
        repo = app.state.repository
        async with repo.transaction() as db:
            await db.execute(
                "UPDATE requests SET status=?, activated_at=10, updated_at=20 WHERE id=?",
                (status, request_id),
            )
        response = await client.post(f"/v1/requests/{request_id}/process")
        assert response.status_code == 200
        assert response.json() == {"id": request_id, "status": status}
        record = await repo.get(request_id)
        assert record.activated_at == 10
        assert record.user_input == "  original text  "
        async with repo.db.execute(
            "SELECT updated_at FROM requests WHERE id=?", (request_id,)
        ) as c:
            assert (await c.fetchone())[0] == 20


async def test_commit_failure_rolls_back_creation(tmp_path):
    import sqlite3

    async with client_at(tmp_path / "test.db") as (client, app):
        repo = app.state.repository
        with patch.object(repo.db, "commit", side_effect=sqlite3.OperationalError("disk full")):
            response = await client.post("/v1/requests", json={"user_input": "not accepted"})
        assert response.status_code == 503
        async with repo.db.execute("SELECT count(*) FROM requests") as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert (await client.get("/health/ready")).status_code == 200
