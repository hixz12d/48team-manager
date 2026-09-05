"""Lifecycle-managed dispatcher for queued automatic reauthorization jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.operations import operation_store, unpack_input
from app.application.reauth import reauth_service
from app.core.time import isoformat, utcnow

logger = logging.getLogger(__name__)


class ReauthDispatcher:
    def __init__(self, *, poll_seconds: float = 2.0, heartbeat_seconds: float = 3.0) -> None:
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self._deployment_allowed = False
        self.last_dispatch_at: datetime | None = None
        self.active_operation_id: str | None = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, factory: async_sessionmaker[AsyncSession], *, deployment_allowed: bool) -> None:
        if self.alive:
            return
        self._factory = factory
        self._deployment_allowed = bool(deployment_allowed)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="reauth-dispatcher")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._deployment_allowed:
                    await self.dispatch_once()
            except Exception:  # noqa: BLE001
                logger.exception("reauth dispatcher iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def dispatch_once(self) -> dict[str, Any]:
        if self._factory is None:
            return {"claimed": False}
        async with self._factory() as db:
            row = await operation_store.claim_next_reauth(db)
            if row is None:
                return {"claimed": False}
            payload = unpack_input(row.input_json)
            ticket = str(payload.get("ticket") or "")
            if row.cancel_requested:
                await operation_store.finish(
                    db,
                    row,
                    {"success": False, "status": "cancelled", "error_code": "cancelled", "error": "operation cancelled before execution"},
                )
                await db.commit()
                return {"claimed": True, "operation_id": row.public_id, "cancelled": True}
            if not ticket:
                await operation_store.finish(
                    db,
                    row,
                    {"success": False, "status": "manual_required", "error_code": "oauth_expired", "error": "OAuth ticket is missing"},
                )
                await db.commit()
                return {"claimed": True, "operation_id": row.public_id, "error_code": "oauth_expired"}
            public_id = row.public_id

        self.active_operation_id = public_id
        self.last_dispatch_at = utcnow()
        execution = asyncio.create_task(self._execute(public_id, ticket))
        try:
            while not execution.done():
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=self.heartbeat_seconds)
                except TimeoutError:
                    async with self._factory() as heartbeat_db:
                        heartbeat_row = await operation_store.get_by_public_id(heartbeat_db, public_id)
                        if heartbeat_row is not None and heartbeat_row.cancel_requested:
                            execution.cancel()
                            try:
                                await execution
                            except asyncio.CancelledError:
                                pass
                            await operation_store.finish(
                                heartbeat_db,
                                heartbeat_row,
                                {
                                    "success": False,
                                    "status": "cancelled",
                                    "error_code": "cancelled",
                                    "error": "browser process terminated after cancellation",
                                },
                            )
                            await heartbeat_db.commit()
                            return {"claimed": True, "operation_id": public_id, "cancelled": True}
                        await operation_store.heartbeat_active(heartbeat_db, public_id)
                        await heartbeat_db.commit()
            result = await execution
            return {"claimed": True, "operation_id": public_id, "result": result}
        finally:
            self.active_operation_id = None

    async def _execute(self, public_id: str, ticket: str) -> dict[str, Any]:
        assert self._factory is not None
        try:
            async with self._factory() as db:
                return await reauth_service.run_job(db, public_id, ticket)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reauth operation failed operation_id=%s", public_id)
            async with self._factory() as db:
                row = await operation_store.get_by_public_id(db, public_id)
                if row is not None and row.state in {"running", "waiting"}:
                    await operation_store.finish(
                        db,
                        row,
                        {"success": False, "error_code": "dispatcher_failed", "error": str(exc), "status": "failed"},
                    )
                    await db.commit()
            return {"success": False, "error_code": "dispatcher_failed"}

    async def summary(self) -> dict[str, Any]:
        payload = {
            "dispatcher_alive": self.alive,
            "deployment_allowed": self._deployment_allowed,
            "last_dispatch_at": isoformat(self.last_dispatch_at),
            "active_browser_operation": self.active_operation_id,
            "queued_count": 0,
            "stale_lease_count": 0,
        }
        if self._factory is not None:
            async with self._factory() as db:
                payload.update(await operation_store.runtime_summary(db))
            if self.active_operation_id:
                payload["active_browser_operation"] = self.active_operation_id
        return payload


reauth_dispatcher = ReauthDispatcher()
