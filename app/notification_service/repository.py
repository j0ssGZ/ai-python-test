import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiosqlite

from notification_service.domain import (
    BacklogFull,
    NotFound,
    ProcessingError,
    RequestRecord,
    Status,
    WorkItem,
)
from notification_service.extraction import PARSER_VERSION, ParsedExtraction
from notification_service.prompt import PROMPT_VERSION

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

MIGRATION_2 = """
BEGIN IMMEDIATE;
ALTER TABLE requests ADD COLUMN stage TEXT NOT NULL DEFAULT 'extract';
ALTER TABLE requests ADD COLUMN ai_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN lease_token TEXT;
ALTER TABLE requests ADD COLUMN lease_expires_at REAL;
ALTER TABLE requests ADD COLUMN extracted_json TEXT;
ALTER TABLE requests ADD COLUMN prompt_version TEXT;
ALTER TABLE requests ADD COLUMN parser_version TEXT;
ALTER TABLE requests ADD COLUMN repaired INTEGER;
ALTER TABLE requests ADD COLUMN provider_id TEXT;
ALTER TABLE requests ADD COLUMN failure_category TEXT;
ALTER TABLE requests ADD COLUMN failure_code TEXT;
CREATE INDEX due_requests ON requests(stage,next_attempt_at,created_at)
    WHERE activated_at IS NOT NULL AND status IN ('queued','processing');
CREATE TABLE attempts (
    request_id TEXT NOT NULL REFERENCES requests(id),
    stage TEXT NOT NULL,
    number INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT NOT NULL DEFAULT 'started',
    failure_category TEXT,
    failure_code TEXT,
    provider_id TEXT,
    PRIMARY KEY(request_id,stage,number)
);
CREATE TABLE rate_admissions (admitted_at REAL NOT NULL);
CREATE INDEX admission_times ON rate_admissions(admitted_at);
CREATE TABLE cooldowns (stage TEXT PRIMARY KEY, not_before REAL NOT NULL);
INSERT INTO cooldowns VALUES('extract',0),('notify',0);
PRAGMA user_version=2;
COMMIT;
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
            await self.db.execute("PRAGMA foreign_keys=ON")
            async with self.db.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            if version > 2:
                raise RuntimeError("Database schema is newer than this application")
            if version == 0:
                await self.db.executescript(SCHEMA)
            if version < 2:
                await self.db.executescript(MIGRATION_2)
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

    async def has_due(self, stage: str, now: float) -> bool:
        async with self._lock:
            async with self.db.execute(
                """SELECT 1 FROM requests WHERE stage=? AND activated_at IS NOT NULL
                   AND status IN ('queued','processing') AND next_attempt_at<=?
                   AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                   AND (SELECT not_before FROM cooldowns WHERE stage=?)<=? LIMIT 1""",
                (stage, now, now, stage, now),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def claim(
        self, stage: str, now: float, lease_seconds: float, max_attempts: int
    ) -> WorkItem | None:
        if stage not in ("extract", "notify"):
            raise ValueError("Unknown stage")
        counter = "ai_attempts" if stage == "extract" else "notify_attempts"
        async with self.transaction() as db:
            await db.execute(
                """UPDATE attempts SET outcome='abandoned',finished_at=?,
                   failure_category='infrastructure',failure_code='lease_expired'
                   WHERE outcome='started' AND token IN (
                       SELECT lease_token FROM requests WHERE stage=? AND lease_expires_at<=?
                   )""",
                (now, stage, now),
            )
            await db.execute(
                f"""UPDATE requests SET status='failed',updated_at=?,lease_token=NULL,
                    lease_expires_at=NULL,failure_category='infrastructure',
                    failure_code='attempts_exhausted_after_crash'
                    WHERE stage=? AND status='processing' AND {counter}>=?
                    AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                (now, stage, max_attempts, now),
            )
            token = str(uuid4())
            async with db.execute(
                f"""UPDATE requests SET status='processing', {counter}={counter}+1,
                    lease_token=?,lease_expires_at=?,updated_at=? WHERE id=(
                    SELECT id FROM requests WHERE stage=? AND activated_at IS NOT NULL
                    AND status IN ('queued','processing') AND {counter}<? AND next_attempt_at<=?
                    AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                    AND (SELECT not_before FROM cooldowns WHERE stage=?)<=?
                    ORDER BY next_attempt_at,created_at,id LIMIT 1) RETURNING *""",
                (token, now + lease_seconds, now, stage, max_attempts, now, now, stage, now),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            await db.execute(
                "INSERT INTO attempts(request_id,stage,number,token,started_at) VALUES(?,?,?,?,?)",
                (row["id"], stage, row[counter], token, now),
            )
            return WorkItem(
                row["id"],
                row["user_input"],
                stage,
                row[counter],
                token,
                now + lease_seconds,
                row["extracted_json"],
            )

    async def finish(
        self,
        item: WorkItem,
        now: float,
        *,
        extraction: ParsedExtraction | None = None,
        provider_id: str | None = None,
        error: ProcessingError | None = None,
        next_attempt_at: float | None = None,
    ) -> bool:
        if sum(value is not None for value in (extraction, provider_id, error)) != 1:
            raise ValueError("Exactly one outcome is required")
        if extraction is not None and item.stage != "extract":
            raise ValueError("Extraction outcome in wrong stage")
        if provider_id is not None and item.stage != "notify":
            raise ValueError("Delivery outcome in wrong stage")
        status = (
            "sent"
            if provider_id
            else ("failed" if error and next_attempt_at is None else "processing")
        )
        stage = "notify" if extraction else item.stage
        async with self.transaction() as db:
            cursor = await db.execute(
                """UPDATE requests SET status=?,stage=?,updated_at=?,next_attempt_at=?,
                   lease_token=NULL,lease_expires_at=NULL,
                   extracted_json=COALESCE(?,extracted_json),
                   prompt_version=COALESCE(?,prompt_version),parser_version=COALESCE(?,parser_version),
                   repaired=COALESCE(?,repaired),provider_id=?,failure_category=?,failure_code=?
                   WHERE id=? AND stage=? AND status='processing' AND lease_token=?
                   AND lease_expires_at>?""",
                (
                    status,
                    stage,
                    now,
                    next_attempt_at if next_attempt_at is not None else now,
                    extraction.notification.model_dump_json() if extraction else None,
                    PROMPT_VERSION if extraction else None,
                    PARSER_VERSION if extraction else None,
                    int(extraction.repaired) if extraction else None,
                    provider_id,
                    error.category if error else None,
                    error.code if error else None,
                    item.id,
                    item.stage,
                    item.token,
                    now,
                ),
            )
            saved = cursor.rowcount == 1
            await cursor.close()
            if saved:
                await db.execute(
                    """UPDATE attempts SET finished_at=?,outcome=?,
                       failure_category=?,failure_code=?,
                       provider_id=? WHERE token=?""",
                    (
                        now,
                        "failed" if error else "succeeded",
                        error.category if error else None,
                        error.code if error else None,
                        provider_id,
                        item.token,
                    ),
                )
            return saved

    async def reserve_rate_slot(
        self, now: float, limit: int, window: float, spacing: float
    ) -> float:
        async with self.transaction() as db:
            await db.execute(
                "DELETE FROM rate_admissions WHERE admitted_at<=?", (now - window - 0.001,)
            )
            async with db.execute("SELECT not_before FROM cooldowns WHERE stage='notify'") as c:
                deadline = max(now, (await c.fetchone())[0])
            async with db.execute(
                "SELECT count(*),min(admitted_at),max(admitted_at) FROM rate_admissions"
            ) as c:
                count, oldest, latest = await c.fetchone()
            if count >= limit:
                deadline = max(deadline, oldest + window + 0.001)
            if latest is not None:
                deadline = max(deadline, latest + spacing)
            if deadline > now:
                return deadline - now
            await db.execute("INSERT INTO rate_admissions VALUES(?)", (now,))
            return 0

    async def cooldown(self, stage: str, deadline: float) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE cooldowns SET not_before=max(not_before,?) WHERE stage=?", (deadline, stage)
            )
