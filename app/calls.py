"""Call bookkeeping: the active-call registry (capacity limit, graceful shutdown) and the call
recorder that stores every call, transcript line, tool call and latency metric in Postgres.

Recording is best-effort and off the audio path: events go onto a queue drained by a
background task, and a database problem is logged, never raised into the call.
"""

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from . import db

if TYPE_CHECKING:
    from .session import CallSession

log = logging.getLogger(__name__)

ACTIVE: dict[str, "CallSession"] = {}


def active_count() -> int:
    return len(ACTIVE)


async def close_all() -> None:
    """Called on shutdown so every call gets its record finalised."""
    for session in list(ACTIVE.values()):
        session.outcome = "server_shutdown"
        await session.close()


class CallRecorder:
    def __init__(self, call_id: str, transport: str, caller_phone: str):
        self.call_id = call_id
        self._transport = transport
        self._caller_phone = caller_phone
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self._latencies: list[int] = []
        self.turns = 0
        self._started = False

    async def start(self, customer_id: int | None) -> None:
        try:
            async with db.pool().acquire() as c:
                await c.execute(
                    """INSERT INTO calls (id, transport, caller_phone, customer_id) VALUES ($1, $2, $3, $4)
                       ON CONFLICT (id) DO NOTHING""",
                    self.call_id,
                    self._transport,
                    self._caller_phone,
                    customer_id,
                )
            self._started = True
        except Exception:
            log.exception("[%s] could not create call record", self.call_id)
        self._writer = asyncio.create_task(self._drain())

    async def event(self, kind: str, text: str, data: dict | None = None) -> None:
        if kind == "caller":
            self.turns += 1
        self._queue.put_nowait((kind, text, data or None))

    def latency(self, ms: int) -> None:
        self._latencies.append(ms)
        self._queue.put_nowait(("metric", "response_latency_ms", {"ms": ms}))

    async def finish(self, outcome: str, verified: bool, models_used: set[str]) -> None:
        if self._writer is not None:
            self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer, timeout=5)
            except asyncio.TimeoutError:
                self._writer.cancel()
        if not self._started:
            return
        avg = round(sum(self._latencies) / len(self._latencies)) if self._latencies else None
        try:
            async with db.pool().acquire() as c:
                await c.execute(
                    """UPDATE calls SET ended_at = now(), outcome = $2, verified = $3, turns = $4,
                              avg_response_ms = $5, models_used = $6 WHERE id = $1""",
                    self.call_id,
                    outcome,
                    verified,
                    self.turns,
                    avg,
                    sorted(models_used),
                )
        except Exception:
            log.exception("[%s] could not finalise call record", self.call_id)

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if not self._started:
                continue
            kind, text, data = item
            try:
                async with db.pool().acquire() as c:
                    await c.execute(
                        "INSERT INTO call_events (call_id, kind, text, data) VALUES ($1, $2, $3, $4::jsonb)",
                        self.call_id,
                        kind,
                        text,
                        json.dumps(data, default=str) if data is not None else None,
                    )
            except Exception:
                log.exception("[%s] could not record %s event", self.call_id, kind)


class Stopwatch:
    """Time from the caller finishing a sentence to the bot's first audio."""

    def __init__(self):
        self._t0: float | None = None

    def start(self) -> None:
        self._t0 = time.monotonic()

    def stop(self) -> int | None:
        if self._t0 is None:
            return None
        ms, self._t0 = round((time.monotonic() - self._t0) * 1000), None
        return ms
