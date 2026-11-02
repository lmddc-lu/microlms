"""Periodic background tasks run inside the application lifespan.

Client projects describe recurring work as :class:`PeriodicTask` items and
pass them to ``create_app(schedules=[...])``; the core runs each task once
at startup and then every interval, for as long as the app lives. A
PostgreSQL advisory lock keyed on the task name collapses concurrent
firings when several processes serve the same database (e.g. multiple
uvicorn workers), so at most one process executes a given tick. Tasks must
still be idempotent: processes started at different times fire at
different instants, so the lock reduces duplicate runs without ruling
them out.
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

# pg_try_advisory_lock takes a bigint; task names are folded into the
# positive int63 range. A collision only serializes unrelated tasks.
_INT63_MASK = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    """A named task run once at startup and then every ``interval``.

    ``run`` executes in a worker thread, so blocking work (database
    access, SMTP) is fine. A task that uses the database opens its
    sessions from ``microlms.create_session_factory`` over the engine
    passed to ``create_app``, so it shares the app's connection pool. A
    failing run is logged and the schedule keeps going.
    """

    name: str
    interval: timedelta
    run: Callable[[], None]


def _lock_key(name: str) -> int:
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big") & _INT63_MASK


def _run_locked(engine: Engine, task: PeriodicTask) -> None:
    """Run the task while holding its advisory lock, or skip the tick when
    another process holds it."""
    with engine.connect() as connection:
        won = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _lock_key(task.name)}
            ).scalar()
        )
        if not won:
            logger.info(
                "periodic task tick skipped: another process holds its lock",
                extra={"task": task.name},
            )
            return
        try:
            task.run()
        finally:
            # Closing the connection would release the lock anyway; the
            # explicit unlock just keeps the connection reusable
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _lock_key(task.name)}
            )


async def run_periodic(engine: Engine, task: PeriodicTask) -> None:
    """Run the task now and then every interval, until cancelled.

    A failing run is logged and the loop continues: one bad tick must
    not silently end the schedule for the rest of the process lifetime.
    """
    while True:
        try:
            await asyncio.to_thread(_run_locked, engine, task)
        except Exception:
            logger.exception("periodic task run failed", extra={"task": task.name})
        await asyncio.sleep(task.interval.total_seconds())
