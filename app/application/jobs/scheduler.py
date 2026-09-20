"""In-process scheduler. Auto reauth / rotate stay off until explicitly enabled."""

from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()
_session_factory = None
last_heartbeat_at = None


def in_test_process() -> bool:
    argv = " ".join(sys.argv).lower()
    return "unittest" in argv or "pytest" in argv


async def scheduled_quota_probe() -> None:
    from app.application.quota import quota_service

    factory = _session_factory
    if factory is None:
        return
    async with factory() as session:
        await quota_service.run_probe_once(session)


async def scheduled_auth_probe() -> None:
    from app.application.tokens import auth_service

    factory = _session_factory
    if factory is None:
        return
    async with factory() as session:
        await auth_service.run_probe_once(session)


async def scheduled_auto_reauth() -> None:
    from app.application.reauth import reauth_service

    factory = _session_factory
    if factory is None:
        return
    async with factory() as session:
        await reauth_service.run_once(session)


async def scheduled_auto_rotate() -> None:
    import json
    from app.application.rotate import rotate_service
    from app.application.automatic_rotation import retry_pending_publish
    from app.application.settings import upsert_setting
    from app.core.time import isoformat, utcnow

    factory = _session_factory
    if factory is None:
        return
    async with factory() as session:
        cfg = await rotate_service.load_settings(session)
        if not cfg["auto_rotate_enabled"]:
            return
        started = isoformat(utcnow())
        await upsert_setting(session, "auto_rotate_last_scan", json.dumps({"started_at": started, "state": "running"}))
        await session.commit()
        try:
            await retry_pending_publish(session)
            # Re-read the switch: disabling during a sync retry must prevent a new rotation.
            stats = await rotate_service.run_once(session)
            summary = {key: stats.get(key) for key in ("scanned", "rotated", "kicked_only", "skipped", "failed", "capped", "conflict")}
            summary["state"] = "completed"
        except Exception:
            logger.exception("automatic rotation scan failed")
            await session.rollback()
            summary = {"state": "failed", "error_code": "scan_failed"}
        summary.update(started_at=started, finished_at=isoformat(utcnow()))
        await upsert_setting(session, "auto_rotate_last_scan", json.dumps(summary))
        await session.commit()


async def scheduled_sub2api_usage_sync() -> None:
    from app.application.sub2api_usage import sub2api_usage_service
    from app.integrations.sub2api.client import sub2api_client

    factory = _session_factory
    if factory is None:
        return
    async with factory() as session:
        config = await sub2api_client.load_config(session)
        if not config.get("configured"):
            return
        await sub2api_usage_service.sync(session, source="scheduled", force_usage=False)


async def scheduled_sub2api_status_sync() -> None:
    from app.application.sub2api_status import refresh
    if _session_factory is not None:
        async with _session_factory() as session:
            await refresh(session)


async def dispatch_quota_queue() -> None:
    from app.application.quota import quota_service
    if _session_factory is not None:
        async with _session_factory() as session:
            await quota_service.run_queued_once(session)


async def record_runtime_heartbeat() -> None:
    global last_heartbeat_at
    from app.core.time import utcnow
    last_heartbeat_at = utcnow()


async def dispatch_workspace_queue() -> None:
    from app.application.jobs.workspace_sync import dispatch_workspace_sync
    if _session_factory is not None:
        async with _session_factory() as session:
            await dispatch_workspace_sync(session)


def configure_jobs(settings: Settings) -> None:
    job_ids = (
        "official_quota_probe_scan",
        "quota_queue_dispatch",
        "workspace_queue_dispatch",
        "runtime_heartbeat",
        "auth_probe_scan",
        "auto_reauth_scan",
        "auto_rotate_scan",
        "sub2api_usage_sync",
        "sub2api_status_sync",
    )
    for job_id in job_ids:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    scheduler.add_job(record_runtime_heartbeat, IntervalTrigger(seconds=5), id="runtime_heartbeat", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(scheduled_quota_probe, IntervalTrigger(minutes=1), id="official_quota_probe_scan", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(dispatch_quota_queue, IntervalTrigger(seconds=2), id="quota_queue_dispatch", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(dispatch_workspace_queue, IntervalTrigger(seconds=2), id="workspace_queue_dispatch", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(scheduled_auth_probe, IntervalTrigger(minutes=1), id="auth_probe_scan", replace_existing=True)
    scheduler.add_job(scheduled_sub2api_status_sync, IntervalTrigger(seconds=15), id="sub2api_status_sync", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(scheduled_auto_reauth, IntervalTrigger(minutes=30), id="auto_reauth_scan", replace_existing=True)
    scheduler.add_job(scheduled_auto_rotate, IntervalTrigger(minutes=1), id="auto_rotate_scan", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(
        scheduled_sub2api_usage_sync,
        IntervalTrigger(minutes=5),
        id="sub2api_usage_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.force_refill:
        logger.warning("force refill is enabled by deployment configuration")
    if settings.auto_reauth_enabled:
        logger.warning("auto reauth is enabled; Playwright stays globally serial")


def start_scheduler(settings: Settings, session_factory=None) -> None:
    global _session_factory, last_heartbeat_at
    last_heartbeat_at = None
    _session_factory = session_factory
    configure_jobs(settings)
    if in_test_process():
        return
    if not scheduler.running:
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
