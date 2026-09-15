"""Global Playwright slot and cancellable browser workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import multiprocessing
from queue import Empty
import time
from typing import Any

_LOCK = asyncio.Lock()


async def run_exclusive(fn: Callable[..., Any], /, *args, **kwargs) -> Any:
    async with _LOCK:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _reauth_process_main(events, kwargs: dict[str, Any], commands=None) -> None:
    try:
        from app.integrations.openai.browser.reauth import run_browser_oauth_reauth
        from app.integrations.openai.browser.onboard import run_browser_onboard
        # Browser workers must not log receipt URLs, proxies or OAuth callback secrets.
        import logging
        logging.disable(logging.CRITICAL)

        def report(stage: str, message: str) -> None:
            events.put({"type": "stage", "stage": str(stage), "message": str(message)[:500]})

        continued = False

        def continue_in_browser(result, browser, page):
            nonlocal continued
            continued = True
            events.put({"type": "result", "result": result})
            next_kwargs = commands.get()
            if next_kwargs is None:
                return
            # A live context belongs to one identity and one frozen browser/proxy setup.
            if any(next_kwargs.get(key, "") != kwargs.get(key, "")
                   for key in ("email", "proxy", "executable_path")):
                events.put({"type": "result", "result": {
                    "ok": False, "error_code": "browser_session_mismatch",
                    "error": "Browser session identity or configuration changed",
                }})
                return
            report("oauth_same_session", "在邀请注册的同一浏览器和页面继续授权")
            oauth = run_browser_oauth_reauth(
                **next_kwargs, existing_browser=browser, existing_page=page,
                on_stage=report, phone_source=None,
            )
            events.put({"type": "result", "result": oauth})

        if kwargs.pop("_invite_onboard", False):
            result = run_browser_onboard(
                **kwargs, on_stage=report, phone_source=None,
                continue_in_browser=continue_in_browser if commands is not None else None,
            )
        else:
            result = run_browser_oauth_reauth(**kwargs, on_stage=report, phone_source=None)
        if not continued:
            events.put({"type": "result", "result": result})
    except BaseException as exc:  # noqa: BLE001
        events.put({"type": "result", "result": {
            "ok": False, "error_code": "browser_worker_failed",
            "error": f"browser worker failed: {exc.__class__.__name__}",
        }})


class InvitedBrowserSession:
    """Keep the worker alive while the parent verifies membership before OAuth.

    Starts lazily, holds the global slot until close, and never restarts a lost
    registration worker to silently authorize in a different browser session.
    """

    def __init__(self):
        self.process = None
        self.events = None
        self.commands = None
        self._locked = False
        self._continued = False
        self._closed = False

    async def run(self, *, on_stage=None, **kwargs):
        if self._closed:
            return {"ok": False, "error_code": "browser_session_lost",
                    "error": "Browser session has been closed"}
        kwargs.pop("phone_source", None)
        if self.process is None:
            await _LOCK.acquire()
            self._locked = True
            context = multiprocessing.get_context("spawn")
            self.events = context.Queue()
            self.commands = context.Queue()
            self.process = context.Process(
                target=_reauth_process_main,
                args=(self.events, kwargs, self.commands), daemon=False,
            )
            self.process.start()
        else:
            if self._continued or kwargs.get("_invite_onboard") or not self.process.is_alive():
                return {"ok": False, "error_code": "browser_session_lost",
                        "error": "Invitation browser session is no longer available"}
            self._continued = True
            self.commands.put(kwargs)
        last_heartbeat = time.monotonic()
        dead_polls = 0
        while True:
            try:
                event = await asyncio.to_thread(self.events.get, True, 0.5)
            except Empty:
                if time.monotonic() - last_heartbeat >= 30:
                    await self._notify(on_stage, "heartbeat", "")
                    last_heartbeat = time.monotonic()
                if self.process.is_alive():
                    continue
                dead_polls += 1
                if dead_polls < 2:
                    continue
                return {"ok": False, "error_code": "browser_worker_exited",
                        "error": "browser worker exited without a result"}
            dead_polls = 0
            if event.get("type") == "stage":
                await self._notify(on_stage, str(event.get("stage") or "browser"), str(event.get("message") or ""))
            if event.get("type") == "result":
                return dict(event.get("result") or {})

    @staticmethod
    async def _notify(callback, stage, message):
        if callback:
            pending = callback(stage, message)
            if inspect.isawaitable(pending):
                await pending

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.process is not None and self.process.pid is not None:
                if self.process.is_alive():
                    self.commands.put(None)
                await asyncio.to_thread(self.process.join, 5)
                if self.process.is_alive():
                    self.process.terminate()
                    await asyncio.to_thread(self.process.join, 5)
                if self.process.is_alive():
                    self.process.kill()
                    await asyncio.to_thread(self.process.join, 5)
        finally:
            for queue in (self.events, self.commands):
                if queue is not None:
                    # A terminated worker may not consume queued commands.
                    queue.cancel_join_thread()
                    queue.close()
            if self._locked:
                self._locked = False
                _LOCK.release()


async def run_reauth_isolated(*, on_stage=None, **kwargs: Any) -> dict[str, Any]:
    session = InvitedBrowserSession()
    try:
        return await session.run(on_stage=on_stage, **kwargs)
    finally:
        await session.close()


async def run_onboard_isolated(**kwargs: Any) -> dict[str, Any]:
    return await run_reauth_isolated(_invite_onboard=True, **kwargs)


def lock() -> asyncio.Lock:
    return _LOCK
