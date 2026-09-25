"""Extension handoff API and after-authorization follow-ups (Sub2API push, switch count)."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tokens import encrypt_secret
from app.core.time import utcnow
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from app.persistence.models.operations import Operation
from tests.helpers import make_client

TOKEN = "t" * 32
WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"
OTHER_UUID = "22222222-2222-2222-2222-222222222222"


def fake_jwt(email: str, account_id: str = WORKSPACE_UUID) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    claims = {"https://api.openai.com/profile": {"email": email},
              "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def callback_for(authorize_url: str, code: str = "code-1") -> str:
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    return "http://localhost:1455/auth/callback?" + urlencode({"code": code, "state": state})


class MemberHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = make_client(Path(self.tmp.name), extension_api_token=TOKEN)
        self.client = self.context.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'team48.db').as_posix()}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            owner = Account(email="owner@example.com", local_purpose="mother", auth_state="healthy",
                            operational_state="active", access_token_encrypted=encrypt_secret("owner-at"))
            db.add(owner)
            await db.flush()
            workspace = Workspace(name="Alpha Team", official_workspace_id=WORKSPACE_UUID, owner_account_id=owner.id,
                                  status="active")
            db.add(workspace)
            await db.commit()
            self.workspace_id, self.owner_id = workspace.id, owner.id
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.push = AsyncMock(return_value={"ok": True, "status": "success", "message": "Sub2API 推送完成", "operation_id": "op-push"})
        for target, mock in (("app.application.sub2api_publish.account_sub2api_push", self.push),
                             ("app.application.quota.quota_service.enqueue_after_credentials", AsyncMock())):
            patcher = patch(target, new=mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.token_sync = AsyncMock(return_value={"ok": True, "skipped": True, "outcome": "not_bound"})
        patcher = patch("app.application.reauth.push_refreshed_tokens_to_bound_sub2api", new=self.token_sync)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.context.__exit__(None, None, None)
        await self.engine.dispose()
        self.tmp.cleanup()

    def exchange(self, email: str, account_id: str = WORKSPACE_UUID):
        at = fake_jwt(email, account_id)
        return patch("app.application.reauth.chatgpt_client.exchange_oauth_code",
                     new=AsyncMock(return_value={"success": True, "access_token": at, "refresh_token": "rt", "id_token": at}))

    async def snapshot(self, email: str, state: str = "joined"):
        async with self.sessions() as db:
            db.add(WorkspaceOfficialMemberSnapshot(workspace_id=self.workspace_id, normalized_email=email,
                                                   official_role="member", remote_state=state, fetched_at=utcnow()))
            await db.commit()

    async def finish_sync(self, operation_id: str, state: str = "success"):
        async with self.sessions() as db:
            row = await db.scalar(__import__("sqlalchemy").select(Operation).where(Operation.public_id == operation_id))
            row.state = state
            row.finished_at = utcnow()
            await db.commit()

    def handoff(self, **body):
        return self.client.post("/api/ext/handoff", headers=self.auth,
                                json={"email": "kid@icloud.com", "workspace_id": self.workspace_id, **body}).json()

    async def run_handoff(self, email="kid@icloud.com"):
        started = self.handoff(email=email)
        self.assertEqual(started["state"], "syncing", started)
        await self.finish_sync(started["operation_id"])
        return self.handoff(email=email, sync_operation_id=started["operation_id"])

    def complete(self, ready, **body):
        return self.client.post("/api/ext/handoff/complete", headers=self.auth, json={
            "account_id": ready["account_id"], "workspace_id": self.workspace_id, "ticket": ready["ticket"],
            "callback_url": callback_for(ready["authorize_url"]), **body}).json()

    def switch_count(self):
        return self.client.get("/api/workspaces").json()["items"][0]["switch_count"]["count"]

    async def test_token_is_required_and_disabled_without_configuration(self):
        self.assertEqual(self.client.get("/api/ext/workspaces").status_code, 401)
        self.assertEqual(self.client.get("/api/ext/workspaces", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.client.cookies.clear()
        items = self.client.get("/api/ext/workspaces", headers=self.auth).json()["items"]
        self.assertEqual([item["name"] for item in items], ["Alpha Team"])

    async def test_full_handoff_links_authorizes_pushes_and_counts_once(self):
        await self.snapshot("kid@icloud.com")
        ready = await self.run_handoff()
        self.assertEqual(ready["state"], "authorize", ready)
        self.assertIn("login_hint=kid%40icloud.com", ready["authorize_url"])
        self.assertEqual(ready["workspace"]["name"], "Alpha Team")
        with self.exchange("kid@icloud.com"):
            done = self.complete(ready)
        self.assertTrue(done["ok"], done)
        self.assertTrue(done["followups"]["sub2api"]["ok"])
        self.assertTrue(done["followups"]["switch_count"]["counted"])
        self.assertEqual(self.push.await_args.kwargs["workspace_id"], self.workspace_id)
        self.assertEqual(self.switch_count(), 1)
        # A second authorization of the same member never counts again.
        again = await self.run_handoff()
        with self.exchange("kid@icloud.com"):
            second = self.complete(again)
        self.assertTrue(second["ok"], second)
        self.assertFalse(second["followups"]["switch_count"]["counted"])
        self.assertEqual(self.switch_count(), 1)

    async def test_invited_or_missing_member_is_not_linked(self):
        missing = await self.run_handoff()
        self.assertEqual(missing["error_code"], "member_not_found")
        await self.snapshot("kid@icloud.com", state="invited")
        invited = await self.run_handoff()
        self.assertEqual(invited["error_code"], "member_not_joined")
        async with self.sessions() as db:
            self.assertIsNone(await db.scalar(__import__("sqlalchemy").select(Account).where(Account.email == "kid@icloud.com")))

    async def test_failed_sync_and_owner_are_refused(self):
        started = self.handoff()
        await self.finish_sync(started["operation_id"], state="failed")
        self.assertEqual(self.handoff(sync_operation_id=started["operation_id"])["error_code"], "sync_failed")
        await self.snapshot("owner@example.com")
        self.assertEqual((await self.run_handoff(email="owner@example.com"))["error_code"], "owner_account")

    async def test_wrong_email_or_state_does_not_push_or_count(self):
        await self.snapshot("kid@icloud.com")
        ready = await self.run_handoff()
        with self.exchange("someone-else@icloud.com"):
            failed = self.complete(ready)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_code"], "token_identity_mismatch")
        forged = dict(ready, authorize_url=ready["authorize_url"].replace("state=", "state=x"))
        self.assertEqual(self.complete(forged)["error_code"], "callback_invalid")
        self.push.assert_not_awaited()
        self.assertEqual(self.switch_count(), 0)

    async def test_token_for_another_workspace_skips_followups(self):
        await self.snapshot("kid@icloud.com")
        ready = await self.run_handoff()
        with self.exchange("kid@icloud.com", account_id=OTHER_UUID):
            done = self.complete(ready)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["followups"]["sub2api"]["error_code"], "workspace_mismatch")
        self.assertEqual(done["followups"]["switch_count"]["error_code"], "workspace_mismatch")
        self.push.assert_not_awaited()
        self.assertEqual(self.switch_count(), 0)

    async def test_bound_account_reuses_token_sync_instead_of_pushing(self):
        self.token_sync.return_value = {"ok": True, "outcome": "synced", "remote_id": 5}
        await self.snapshot("kid@icloud.com")
        ready = await self.run_handoff()
        with self.exchange("kid@icloud.com"):
            done = self.complete(ready)
        self.assertTrue(done["followups"]["sub2api"]["skipped"])
        self.push.assert_not_awaited()
        self.assertEqual(self.switch_count(), 1)

    async def test_console_reauth_complete_followups_are_opt_in(self):
        await self.snapshot("kid@icloud.com")
        ready = await self.run_handoff()
        with self.exchange("kid@icloud.com"):
            plain = self.client.post(f"/api/accounts/{ready['account_id']}/reauth/complete", json={
                "ticket": ready["ticket"], "callback_url": callback_for(ready["authorize_url"])}).json()
        self.assertTrue(plain["ok"], plain)
        self.assertNotIn("followups", plain)
        self.push.assert_not_awaited()
        started = self.client.post(f"/api/accounts/{ready['account_id']}/reauth").json()
        with self.exchange("kid@icloud.com"):
            full = self.client.post(f"/api/accounts/{ready['account_id']}/reauth/complete", json={
                "ticket": started["ticket"], "callback_url": callback_for(started["authorize_url"]),
                "workspace_id": self.workspace_id, "push_sub2api": True, "count_switch": True}).json()
        self.assertTrue(full["followups"]["sub2api"]["ok"])
        self.assertEqual(full["followups"]["switch_count"]["switch_count"]["count"], 1)

    async def test_owner_reauth_from_console_never_counts(self):
        started = self.client.post(f"/api/accounts/{self.owner_id}/reauth").json()
        with self.exchange("owner@example.com"):
            done = self.client.post(f"/api/accounts/{self.owner_id}/reauth/complete", json={
                "ticket": started["ticket"], "callback_url": callback_for(started["authorize_url"]),
                "workspace_id": self.workspace_id, "push_sub2api": True, "count_switch": True}).json()
        self.assertTrue(done["ok"], done)
        self.assertIsNone(done["followups"]["switch_count"]["ok"])
        self.push.assert_not_awaited()
        self.assertEqual(self.switch_count(), 0)


class ExtensionDisabledTests(unittest.TestCase):
    def test_endpoints_are_hidden_without_a_long_token(self):
        for token in ("", "short-token"):
            with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp), extension_api_token=token) as client:
                self.assertEqual(client.get("/api/ext/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code, 404)


class MembershipBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_authorized_members_are_marked_counted_on_upgrade(self):
        from sqlalchemy import text
        from app.persistence.migrations.bootstrap import bootstrap_schema
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(tmp.name) / 'old.db').as_posix()}")
        try:
            await bootstrap_schema(engine)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                authorized = Account(email="a@x.com", local_purpose="child", auth_state="healthy",
                                     operational_state="active", access_token_encrypted="tok")
                pending = Account(email="b@x.com", local_purpose="child", auth_state="oauth_required", operational_state="active")
                workspace = Workspace(name="Old", status="active")
                db.add_all([authorized, pending, workspace])
                await db.flush()
                db.add_all([WorkspaceMembership(workspace_id=workspace.id, account_id=row.id, official_role="member",
                                                membership_state="joined", local_purpose="child") for row in (authorized, pending)])
                await db.commit()
                ids = (authorized.id, pending.id)
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE workspace_memberships DROP COLUMN switch_counted_at"))
            await bootstrap_schema(engine)
            async with engine.connect() as conn:
                rows = dict((await conn.execute(text("SELECT account_id, switch_counted_at FROM workspace_memberships"))).fetchall())
        finally:
            await engine.dispose()
        self.assertIsNotNone(rows[ids[0]])
        self.assertIsNone(rows[ids[1]])


if __name__ == "__main__":
    unittest.main()
