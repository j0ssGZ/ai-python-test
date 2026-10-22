"""Diagnose real SQLite writer contention; never run against an application database."""

import argparse
import asyncio
import json
import platform
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from notification_service.repository import SQLiteRepository  # noqa: E402


class TracedRepository(SQLiteRepository):
    def __init__(self, path: Path, label: str, events: list[dict[str, Any]]) -> None:
        super().__init__(str(path))
        self.label, self.events = label, events

    def mark(self, event: str, **details: object) -> None:
        self.events.append(
            {"time": time.perf_counter(), "connection": self.label, "event": event, **details}
        )

    def trace(self, sql: str) -> None:
        if sql in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"):
            self.mark(sql)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        current = asyncio.current_task()
        task = current.get_name() if current is not None else "unknown"
        self.mark("requested", task=task)
        try:
            async with super().transaction() as db:
                self.mark("acquired", task=task)
                yield db
            self.mark("finished", task=task)
        except BaseException as error:
            self.mark("error", task=task, **describe_error(error))
            raise


def describe_error(error: BaseException) -> dict[str, Any]:
    return {
        "type": type(error).__name__,
        "message": str(error),
        "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
        "sqlite_errorname": getattr(error, "sqlite_errorname", None),
    }


async def probe(path: Path, paired: bool) -> dict[str, Any]:
    events, config = [], {}
    async with TracedRepository(path, "a", events) as a, TracedRepository(path, "b", events) as b:
        for repo in (a, b):
            config[repo.label] = {}
            for pragma in ("journal_mode", "synchronous", "busy_timeout", "wal_autocheckpoint"):
                async with repo.db.execute("PRAGMA " + pragma) as cursor:
                    config[repo.label][pragma] = (await cursor.fetchone())[0]
            await repo.db.set_trace_callback(repo.trace)
        started = time.perf_counter()
        if paired:
            barrier = asyncio.Barrier(2)

            async def reserve(repo: SQLiteRepository) -> list[float]:
                results = []
                for _ in range(50):
                    await barrier.wait()
                    results.append(await repo.reserve_rate_slot(100, 50, 10, 0))
                return results

            async with asyncio.TaskGroup() as group:
                first = group.create_task(reserve(a))
                second = group.create_task(reserve(b))
            results = first.result() + second.result()
        else:
            # Settle every task before closing connections, retaining every error.
            # Unlike the historical test, failure cannot leave detached work behind.
            results = await asyncio.gather(
                *[(a if i % 2 else b).reserve_rate_slot(100, 50, 10, 0) for i in range(100)],
                return_exceptions=True,
            )
        elapsed = time.perf_counter() - started
        async with a.db.execute("SELECT count(*) FROM rate_admissions") as cursor:
            persisted = (await cursor.fetchone())[0]
        async with a.db.execute("PRAGMA quick_check") as cursor:
            integrity = (await cursor.fetchone())[0]
        summary = {
            "seconds": elapsed,
            "granted": sum(isinstance(r, (int, float)) and r == 0 for r in results),
            "deferred": sum(isinstance(r, (int, float)) and r > 0 for r in results),
            "errors": [describe_error(r) for r in results if isinstance(r, BaseException)],
            "persisted": persisted,
            "integrity": integrity,
            "transactions_open_after": [a.db.in_transaction, b.db.in_transaction],
            "config": config,
        }
    return {"summary": summary, "events": sorted(events, key=lambda event: event["time"])}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="New scratch directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--paired", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    args.directory.mkdir(parents=True, exist_ok=False)
    report = {
        "metadata": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "aiosqlite": aiosqlite.__version__,
            "directory": str(args.directory.resolve()),
            "paired": args.paired,
        },
        "runs": [],
    }
    for i in range(args.repeats):
        result = await probe(args.directory / f"probe-{i}.db", args.paired)
        report["runs"].append(result)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"run": i + 1, **result["summary"]}), flush=True)
    return int(any(run["summary"]["errors"] for run in report["runs"]))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
