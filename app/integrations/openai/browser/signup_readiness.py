"""Readiness gate for the managed signup -> OAuth handoff.

Uses DOM observations from the isolated world and independent session reads.
It does not infer Team membership or whether OAuth will require a phone.
"""
from __future__ import annotations

import time

HOME_STABLE_SECONDS = 2.0
SESSION_CONFIRM_SECONDS = 2.0
HOME_READY_TIMEOUT = 30.0


class SignupReadiness:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.key = None
        self.since = None
        self.confirmed_at = None
        self.confirmations = 0

    def observe(self, snapshot):
        """A changed document, URL or readiness revision starts a new interval."""
        if not snapshot or not snapshot.get("ready"):
            self.reset()
            return False
        key = (snapshot["document"], snapshot["url"], snapshot["revision"])
        if key != self.key:
            self.reset()
            self.key, self.since = key, self.clock()
        return True

    def confirm_identity(self):
        # Caller must already have checked the expected email and verification state.
        if self.since is None:
            return False
        now = self.clock()
        if self.confirmed_at is None or now - self.confirmed_at >= SESSION_CONFIRM_SECONDS:
            self.confirmed_at = now
            self.confirmations += 1
        return self.confirmations >= 2 and now - self.since >= HOME_STABLE_SECONDS

    def diagnostics(self):
        return {"stable_ms": max(0, int((self.clock() - self.since) * 1000)) if self.since is not None else 0,
                "session_confirmations": self.confirmations}
