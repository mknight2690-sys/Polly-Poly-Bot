"""Self-healing supervisor for POLY background workers."""
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Callable, Coroutine, Optional

from .memory import PolyMemory


class TaskRecord:
    def __init__(self, name: str, factory: Callable[..., Coroutine], heartbeat_timeout: float = 0.0):
        self.name = name
        self.factory = factory
        self.heartbeat_timeout = heartbeat_timeout
        self.task: Optional[asyncio.Task] = None
        self.restarts = 0
        self.last_error = ""
        self.last_beat = time.time()
        self.started_at = time.time()


class Supervisor:
    def __init__(self, memory: PolyMemory):
        self.memory = memory
        self.records: dict[str, TaskRecord] = {}
        self.incidents: list[dict] = []

    def beat(self, name: str):
        rec = self.records.get(name)
        if rec:
            rec.last_beat = time.time()

    def _incident(self, task_name: str, error: str, action: str):
        inc = {"ts": time.time(), "task": task_name, "error": error[:300], "action": action}
        self.incidents.append(inc)
        self.incidents = self.incidents[-300:]
        try:
            self.memory.record_incident(inc)
        except Exception:
            pass

    def spawn(self, name: str, factory: Callable[..., Coroutine], heartbeat_timeout: float = 0.0):
        rec = TaskRecord(name, factory, heartbeat_timeout)
        self.records[name] = rec
        self._start(rec)

    def _start(self, rec: TaskRecord):
        async def runner():
            backoff = 1.0
            while True:
                rec.started_at = time.time()
                rec.last_beat = time.time()
                try:
                    await rec.factory(lambda: self.beat(rec.name))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rec.restarts += 1
                    rec.last_error = f"{e.__class__.__name__}: {e}"
                    self._incident(
                        rec.name,
                        rec.last_error + "\n" + traceback.format_exc()[-400:],
                        f"restart #{rec.restarts} in {backoff:.0f}s",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300.0)

        rec.task = asyncio.get_event_loop().create_task(runner(), name=f"sup:{rec.name}")

    async def watchdog(self, beat=None):
        while True:
            await asyncio.sleep(15)
            for rec in self.records.values():
                dead = rec.task is None or rec.task.done()
                stalled = (
                    rec.heartbeat_timeout > 0
                    and time.time() - rec.last_beat > rec.heartbeat_timeout
                )
                if dead and rec.task and not rec.task.cancelled():
                    try:
                        exc = rec.task.exception()
                    except Exception:
                        exc = None
                    if exc is None:
                        continue
                if stalled and rec.task and not rec.task.done():
                    rec.task.cancel()
                    self._incident(rec.name, "heartbeat stall", "cancelled + restart")
                    dead = True
                if dead:
                    rec.restarts += 1
                    self._incident(
                        rec.name,
                        rec.last_error or "task dead",
                        f"watchdog restart #{rec.restarts}",
                    )
                    self._start(rec)
            if beat:
                beat()

    def status(self) -> dict:
        now = time.time()
        tasks = []
        for rec in self.records.values():
            alive = rec.task is not None and not rec.task.done()
            tasks.append(
                {
                    "name": rec.name,
                    "alive": alive,
                    "restarts": rec.restarts,
                    "last_error": rec.last_error,
                    "age_sec": round(now - rec.started_at, 1),
                    "since_beat_sec": round(now - rec.last_beat, 1),
                }
            )
        return {
            "tasks": tasks,
            "incidents": self.incidents[-12:][::-1],
            "ok": all(t["alive"] for t in tasks) if tasks else False,
        }
