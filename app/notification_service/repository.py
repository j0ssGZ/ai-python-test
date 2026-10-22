import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from notification_service.domain import BacklogFull, NotFound, RequestRecord, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    user_input TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'sent', 'failed')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    activated_at REAL
);
CREATE INDEX IF NOT EXISTS active_requests ON requests(status)
    WHERE status IN ('queued', 'processing');
PRAGMA user_version=1;
"""


class SQLiteRepository:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Repository is not open")
        return self._db

    async def __aenter__(self) -> "SQLiteRepository":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, isolation_level=None, timeout=1)
        self._db.row_factory = aiosqlite.Row
        try:
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA synchronous=FULL")
            await self.db.execute("PRAGMA busy_timeout=1000")
            await self.db.executescript(SCHEMA)
        except BaseException:
            await self.db.close()
            self._db = None
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        async with self._lock:
            await self.db.close()
            self._db = None

    @asynccontextmanager
    async def transaction(self):
        async with self._lock:
            try:
                # BEGIN can execute in aiosqlite's thread even if this task is cancelled.
                await self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    @staticmethod
    def record(row: aiosqlite.Row) -> RequestRecord:
        return RequestRecord(
            row["id"],
            row["user_input"],
            Status(row["status"]),
            row["created_at"],
            row["activated_at"],
        )

    async def create(self, record: RequestRecord, capacity: int) -> None:
        async with self.transaction() as db:
            async with db.execute(
                "SELECT count(*) FROM requests WHERE status IN ('queued', 'processing')"
            ) as cursor:
                count = (await cursor.fetchone())[0]
            if count >= capacity:
                raise BacklogFull
            await db.execute(
                "INSERT INTO requests(id,user_input,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (record.id, record.user_input, record.status, record.created_at, record.created_at),
            )

    async def get(self, request_id: str) -> RequestRecord:
        async with self._lock:
            async with self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            raise NotFound
        return self.record(row)

    async def activate(self, request_id: str, now: float) -> RequestRecord:
        async with self.transaction() as db:
            await db.execute(
                """UPDATE requests SET activated_at=?, updated_at=?
                   WHERE id=? AND activated_at IS NULL AND status='queued'""",
                (now, now, request_id),
            )
            async with db.execute("SELECT * FROM requests WHERE id=?", (request_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise NotFound
        return self.record(row)

    async def ping(self) -> None:
        async with self._lock:
            await self.db.execute("SELECT 1")
