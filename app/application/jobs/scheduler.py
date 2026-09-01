"""In-process scheduler. Auto reauth / rotate stay off until explicitly enabled."""

from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def in_test_process() -> bool:
    argv = " ".join(sys.argv).lower()
    return "unittest" in argv or "pytest" in argv


async def scheduled_quota_probe() -> None:
    from app.application.quota import quota_service
    from app.main import app

    factory = getattr(app.state, "session_factory", None)
    if factory is None:
        return
    async with factory() as session:
        await quota_service.run_probe_once(session)


async def scheduled_auth_probe() -> None:
    from app.application.tokens import auth_service
    from app.main import app

    factory = getattr(app.state, "session_factory", None)
    if factory is None:
        return
    async with factory() as session:
        await auth_service.run_probe_once(session)


async def scheduled_auto_reauth() -> None:
    from app.application.reauth import reauth_service
    from app.main import app

    factory = getattr(app.state, "session_factory", None)
    if factory is None:
        return
    async with factory() as session:
        await reauth_service.run_once(session)


def configure_jobs(settings: Settings) -> None:
    if scheduler.get_job("official_quota_probe_scan"):
        scheduler.remove_job("official_quota_probe_scan")
    if scheduler.get_job("auth_probe_scan"):
        scheduler.remove_job("auth_probe_scan")
    if scheduler.get_job("auto_reauth_scan"):
        scheduler.remove_job("auto_reauth_scan")
    scheduler.add_job(scheduled_quota_probe, IntervalTrigger(minutes=2), id="official_quota_probe_scan", replace_existing=True)
    scheduler.add_job(scheduled_auth_probe, IntervalTrigger(minutes=30), id="auth_probe_scan", replace_existing=True)
    scheduler.add_job(scheduled_auto_reauth, IntervalTrigger(minutes=30), id="auto_reauth_scan", replace_existing=True)
    if settings.auto_rotate_enabled or settings.force_refill:
        logger.warning("auto rotate / force refill must stay off until explicitly approved")
    if settings.auto_reauth_enabled:
        logger.warning("auto reauth is enabled; Playwright stays globally serial")


def start_scheduler(settings: Settings) -> None:
    configure_jobs(settings)
    if in_test_process():
        return
    if not scheduler.running:
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
