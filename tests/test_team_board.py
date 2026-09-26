"""Overview team board data: last authorization and last Sub2API push per account."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import upsert_mother_account, upsert_workspace
from app.application.queries.portfolio import portfolio_query
from app.application.sub2api_publish import remote_pushed_at
from app.persistence.database import Base
from app.persistence.models.identity import ExternalBinding
from app.persistence.models.oauth import OAuthSession

WORKSPACE = "33333333-3333-3333-3333-333333333333"


def _session(account_id, *, consumed_at, status="consumed", public_id):
    stamp = consumed_at or datetime.now(timezone.utc)
    return OAuthSession(
        public_id=public_id, purpose="account_reauth", mode="manual", account_id=account_id,
        email="owner@example.com", state_hash=public_id.ljust(64, "0")[:64], code_verifier_encrypted="x",
        client_id="c", redirect_uri="http://localhost:1455/auth/callback", authorize_url="https://example.invalid",
        status=status, expires_at=stamp + timedelta(minutes=10), consumed_at=consumed_at,
    )


class TeamBoardDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)()
        self.owner, _ = await upsert_mother_account(self.session, email="owner@example.com")
        self.workspace, _ = await upsert_workspace(
            self.session, source_team_id=1, official_workspace_id=WORKSPACE,
            name="Board Team", owner_account_id=self.owner.id,
            subscription_plan="team", status="active", seat_limit=5,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _mother(self):
        payload = await portfolio_query(self.session)
        group = next(g for g in payload["groups"] if g["id"] == self.workspace.id)
        return group["mother"]

    async def test_last_authorized_is_latest_consumed_session_only(self):
        older = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
        self.session.add_all([
            _session(self.owner.id, consumed_at=older, public_id="old"),
            _session(self.owner.id, consumed_at=newer, public_id="new"),
            # Abandoned or failed sessions are not authorizations.
            _session(self.owner.id, consumed_at=None, status="waiting", public_id="waiting"),
            _session(self.owner.id, consumed_at=datetime(2026, 9, 26, tzinfo=timezone.utc), status="failed", public_id="failed"),
        ])
        await self.session.commit()
        mother = await self._mother()
        self.assertTrue(mother["last_authorized_at"].startswith("2026-09-25T09:30"))

    async def test_never_authorized_is_null_not_zero(self):
        mother = await self._mother()
        self.assertIsNone(mother["last_authorized_at"])
        self.assertIsNone(mother["remote_status"].get("pushed_at"))

    async def test_pushed_at_comes_from_binding(self):
        pushed = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        self.session.add(ExternalBinding(
            provider="sub2api", local_account_id=self.owner.id, remote_account_id="42",
            workspace_id=self.workspace.id, binding_state="verified", last_pushed_at=pushed,
        ))
        await self.session.commit()
        mother = await self._mother()
        self.assertEqual(mother["remote_status"]["remote_id"], "42")
        self.assertTrue(mother["remote_status"]["pushed_at"].startswith("2026-09-24T12:00"))

    def test_remote_pushed_at_prefers_sub2api_updated_at(self):
        self.assertEqual(remote_pushed_at({"updated_at": "2026-09-24T12:00:00Z"}), datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(remote_pushed_at({"updatedAt": "2026-09-24T20:00:00+08:00"}).astimezone(timezone.utc).hour, 12)
        # Missing or malformed remote time falls back to the local write time.
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.assertGreater(remote_pushed_at({"updated_at": "not-a-time"}), before)
        self.assertGreater(remote_pushed_at(None), before)


if __name__ == "__main__":
    unittest.main()
