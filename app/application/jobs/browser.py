"""Global Playwright slot. Concurrency is 1."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

_LOCK = asyncio.Lock()


async def run_exclusive(fn: Callable[..., Any], /, *args, **kwargs) -> Any:
    async with _LOCK:
        return await asyncio.to_thread(fn, *args, **kwargs)


def lock() -> asyncio.Lock:
    return _LOCK
