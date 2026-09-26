"""In-process runner for console workspace commands.

The HTTP request validates input, takes the workspace lock and commits the
Operation, then returns its public id. The workflow itself runs here on its own
session. Nothing is resumed after a restart: recover_stale_operations decides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.operations import operation_store
from app.domain.automation import ACTIVE_STATES

logger = logging.getLogger(__name__)

Command = Callable[[AsyncSession], Awaitable[dict[str, Any]]]
COMMAND_FAILED = {
    "success": False,
    "status": "failed",
    "error_code": "command_failed",
    "error": "后台任务异常中断，请核对团队状态后再操作",
}
CANCELLED = {
    "success": False,
    "status": "cancelled",
    "error_code": "cancelled",
    "error": "已在安全点停止；已发出的邀请或已完成的步骤不会自动撤销",
}
INTERRUPTED = {
    "success": False,
    "status": "manual_required",
    "error_code": "resume_manual",
    "error": "服务重启，任务中断；请核对团队状态后再操作",
}


class CommandRunner:
    def __init__(self) -> None:
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def ready(self) -> bool:
        return self._factory is not None

    def start(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def running_ids(self) -> set[str]:
        return set(self._tasks)

    def spawn(self, public_id: str, command: Command) -> None:
        if self._factory is None:
            raise RuntimeError("command runner is not started")
        task = asyncio.create_task(self._execute(self._factory, public_id, command), name=f"command:{public_id}")
        self._tasks[public_id] = task
        task.add_done_callback(lambda _task, key=public_id: self._tasks.pop(key, None))

    async def _execute(self, factory: async_sessionmaker[AsyncSession], public_id: str, command: Command) -> None:
        async with factory() as db:
            try:
                operation = await operation_store.get_by_public_id(db, public_id)
                if operation is None:
                    return
                result = await command(db)
                await operation_store.finish(db, operation, result)
                await db.commit()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # Shutdown: the Playwright/official state is unknown, so hand it to a human.
                    await self._record(factory, public_id, INTERRUPTED)
                    raise
                # Flows raise CancelledError at a safe point after a cancel request.
                await self._record(factory, public_id, CANCELLED)
            except Exception:  # noqa: BLE001
                # Upstream exceptions may carry credentials; only the log keeps details.
                logger.exception("console command failed operation_id=%s", public_id)
                await self._record(factory, public_id, COMMAND_FAILED)

    async def _record(self, factory: async_sessionmaker[AsyncSession], public_id: str, result: dict[str, Any]) -> None:
        try:
            async with factory() as db:
                operation = await operation_store.get_by_public_id(db, public_id)
                if operation is not None and operation.state in ACTIVE_STATES:
                    await operation_store.finish(db, operation, dict(result))
                    await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not record command outcome operation_id=%s", public_id)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._factory = None


command_runner = CommandRunner()
