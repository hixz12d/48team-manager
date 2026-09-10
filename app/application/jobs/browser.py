"""Global Playwright slot and cancellable reauth browser subprocess."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import multiprocessing
from queue import Empty
from typing import Any

_LOCK = asyncio.Lock()


async def run_exclusive(fn: Callable[..., Any], /, *args, **kwargs) -> Any:
    async with _LOCK:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _reauth_process_main(events, kwargs: dict[str, Any]) -> None:
    try:
        if kwargs.pop("_invite_onboard", False):
            from app.integrations.openai.browser.onboard import run_browser_onboard as run_browser
        else:
            from app.integrations.openai.browser.reauth import run_browser_oauth_reauth as run_browser
        # Browser workers must not log receipt URLs, proxies or OAuth callback secrets.
        import logging
        logging.disable(logging.CRITICAL)

        def report(stage: str, message: str) -> None:
            events.put({"type": "stage", "stage": str(stage), "message": str(message)[:500]})

        result = run_browser(**kwargs, on_stage=report, phone_source=None)
        events.put({"type": "result", "result": result})
    except BaseException as exc:  # noqa: BLE001
        events.put(
            {
                "type": "result",
                "result": {
                    "ok": False,
                    "error_code": "browser_worker_failed",
                    "error": f"browser worker failed: {exc.__class__.__name__}",
                },
            }
        )


async def run_reauth_isolated(
    *,
    on_stage: Callable[[str, str], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one reauth browser in its own process; cancellation waits for real exit."""
    async with _LOCK:
        context = multiprocessing.get_context("spawn")
        events = context.Queue()
        process = context.Process(target=_reauth_process_main, args=(events, kwargs), daemon=False)
        process.start()
        try:
            import inspect
            import time

            async def notify(stage, message):
                if on_stage:
                    pending = on_stage(stage, message)
                    if inspect.isawaitable(pending):
                        await pending

            last_heartbeat = time.monotonic()
            dead_polls = 0
            while True:
                try:
                    event = await asyncio.to_thread(events.get, True, 0.5)
                except Empty:
                    if time.monotonic() - last_heartbeat >= 30:
                        await notify("heartbeat", "")
                        last_heartbeat = time.monotonic()
                    if process.is_alive():
                        continue
                    dead_polls += 1
                    if dead_polls < 2:
                        continue
                    await asyncio.to_thread(process.join, 1)
                    return {
                        "ok": False,
                        "error_code": "browser_worker_exited",
                        "error": "browser worker exited without a result",
                    }
                dead_polls = 0
                if event.get("type") == "stage" and on_stage:
                    await notify(str(event.get("stage") or "browser"), str(event.get("message") or ""))
                if event.get("type") == "result":
                    await asyncio.to_thread(process.join, 5)
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 5)
                    return dict(event.get("result") or {})
        except asyncio.CancelledError:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 5)
            raise
        finally:
            events.close()
            events.join_thread()


async def run_onboard_isolated(**kwargs: Any) -> dict[str, Any]:
    kwargs.pop("phone_source", None)
    return await run_reauth_isolated(_invite_onboard=True, **kwargs)


def lock() -> asyncio.Lock:
    return _LOCK
