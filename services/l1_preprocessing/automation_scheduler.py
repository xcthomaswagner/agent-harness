"""Small asyncio scheduler for operator automations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog

from automation_jobs import run_automation_job
from automation_store import (
    ensure_default_jobs,
    finish_run,
    get_job,
    list_due_jobs,
    list_jobs,
    schedule_next_run,
    start_run,
)
from autonomy_store import ensure_schema, open_connection
from autonomy_store.schema import resolve_db_path
from config import settings

logger = structlog.get_logger()


def automation_db_path() -> Path:
    return resolve_db_path(settings.autonomy_db_path)


class AutomationScheduler:
    """Interval scheduler with explicit run history.

    This deliberately avoids cron syntax and external dependencies. The
    dashboard stores steady intervals in SQLite, the loop checks due jobs,
    and each execution writes an automation_runs row plus any events the job
    emits.
    """

    def __init__(self, *, tick_seconds: int = 15) -> None:
        self._tick_seconds = max(1, tick_seconds)
        self._loop_task: asyncio.Task[Any] | None = None
        self._running_jobs: set[str] = set()
        self._run_lock = asyncio.Lock()
        self._child_tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop(), name="automation-scheduler")
        logger.info("automation_scheduler_started", tick_seconds=self._tick_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        for task in list(self._child_tasks):
            task.cancel()
        if self._child_tasks:
            await asyncio.gather(*self._child_tasks, return_exceptions=True)
        logger.info("automation_scheduler_stopped")

    async def _loop(self) -> None:
        self._ensure_seeded()
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "automation_scheduler_tick_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)

    def _ensure_seeded(self) -> None:
        conn = open_connection(automation_db_path())
        try:
            ensure_schema(conn)
            ensure_default_jobs(conn)
        finally:
            conn.close()

    async def _tick(self) -> None:
        conn = open_connection(automation_db_path())
        try:
            ensure_schema(conn)
            due = list_due_jobs(conn)
        finally:
            conn.close()
        for job in due:
            task = asyncio.create_task(
                self.run_now(str(job["job_key"]), triggered_by="scheduler"),
                name=f"automation-{job['job_key']}",
            )
            self._child_tasks.add(task)
            task.add_done_callback(self._child_tasks.discard)

    async def run_now(self, job_key: str, *, triggered_by: str = "operator") -> dict[str, Any]:
        db_path = automation_db_path()
        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            ensure_default_jobs(conn)
            job = get_job(conn, job_key)
            if job is None:
                raise KeyError(job_key)
            async with self._run_lock:
                if job_key in self._running_jobs:
                    run = start_run(conn, job_key, triggered_by=triggered_by)
                    return finish_run(
                        conn,
                        int(run["id"]),
                        status="skipped",
                        summary="Job already running.",
                    )
                self._running_jobs.add(job_key)
            run = start_run(conn, job_key, triggered_by=triggered_by)
        finally:
            conn.close()

        run_id = int(run["id"])
        try:
            summary, details = await asyncio.to_thread(
                run_automation_job,
                job,
                db_path=db_path,
                run_id=run_id,
            )
            status = "succeeded"
            error = ""
        except Exception as exc:
            logger.exception(
                "automation_job_failed",
                job_key=job_key,
                run_id=run_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            summary = "Job failed."
            details = {}
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            async with self._run_lock:
                self._running_jobs.discard(job_key)

        conn = open_connection(db_path)
        try:
            ensure_schema(conn)
            finished = finish_run(
                conn,
                run_id,
                status=status,
                summary=summary,
                details=details,
                error=error,
            )
            schedule_next_run(conn, job_key, int(job["interval_seconds"]))
            return finished
        finally:
            conn.close()

    def snapshot(self) -> dict[str, Any]:
        conn = open_connection(automation_db_path())
        try:
            ensure_schema(conn)
            return {
                "running_jobs": sorted(self._running_jobs),
                "jobs": list_jobs(conn),
            }
        finally:
            conn.close()


_scheduler: AutomationScheduler | None = None


def get_scheduler() -> AutomationScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AutomationScheduler()
    return _scheduler


def start_automation_scheduler() -> None:
    get_scheduler().start()


async def stop_automation_scheduler() -> None:
    if _scheduler is not None:
        await _scheduler.stop()


async def run_automation_now(
    job_key: str,
    *,
    triggered_by: str = "operator",
) -> dict[str, Any]:
    return await get_scheduler().run_now(job_key, triggered_by=triggered_by)
