"""P0/P1 coverage for official members, lookup, pagination, and console loops."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.console_actions import (
    account_refresh,
    account_sub2api_sync,
    retry_operation,
    start_controlled_rotate,
    update_account_proxy,
)
from app.application.identity import upsert_mother_account, upsert_workspace
from app.application.operations import operation_store, pack_input, recover_stale_operations
from app.application.queries.identity import accounts_query, workspaces_query
from app.application.resources.hme import occupied_account_emails, reconcile_aliases
from app.application.resources.phones import phone_pool_service
from app.application.workspace_sync import WorkspaceSyncService
from app.application.workspaces import WorkspaceService
from app.core.time import utcnow
from app.domain.identity import PROVIDER_SUB2API
from app.integrations.openai.chatgpt import ChatGPTClient
from app.integrations.openai.member_adapter import adapt_collection, normalize_official_member, validate_fetch_counts
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceOfficialMemberSnapshot
from app.persistence.models.resources import HmeAliasLease, PhonePool
from tests.helpers import make_client


NESTED_JAMES = {
    "user": {
        "id": "user-james",
        "email": "james.smith@example.com",
        "name": "James Smith",
    },
    "role": "standard-user",
    "seat_type": "member",
    "created_at": "2026-03-01T00:00:00Z",
}


class _FakeWorkspaces:
    def __init__(self, members=None, invites=None):
        self._members = members or {"success": True, "members": [], "total": 0}
        self._invites = invites or {"success": True, "items": [], "total": 0}

    async def load_workspace(self, db, workspace_id: int):
        return await db.get(Workspace, int(workspace_id))

    async def get_members(self, db, workspace):
        return self._members

    async def get_invites(self, db, workspace):
        return self._invites

    async def owner_account(self, db, workspace):
        if workspace.owner_account_id:
            return await db.get(Account, workspace.owner_account_id)
        return None


class MemberAdapterTests(unittest.TestCase):
    def test_nested_and_top_level_variants(self):
        nested = normalize_official_member(NESTED_JAMES)
        self.assertEqual(nested["email"], "james.smith@example.com")
        self.assertEqual(nested["user_id"], "user-james")
        self.assertEqual(nested["name"], "James Smith")
        top = normalize_official_member({"email_address": "a@x.com", "official_role": "account-owner", "id": "user-a"})
        self.assertEqual(top["email"], "a@x.com")
        self.assertEqual(top["role"], "account-owner")

    def test_schema_mismatch_when_reported_but_unparsed(self):
        adapted = adapt_collection([{"id": "user-x", "profile": {"display": "no email"}}], default_state="joined")
        check = validate_fetch_counts(
            reported_total=2,
            raw_item_count=adapted["raw_item_count"],
            parsed_item_count=adapted["parsed_item_count"],
            invalid_item_count=adapted["invalid_item_count"],
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["error_code"], "schema_mismatch")


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_page_duplicate_and_max_pages(self):
        client = ChatGPTClient()
        pages = [
            {"success": True, "data": {"items": [{"email": "a@x.com", "id": "user-a"}], "total": 3}},
            {"success": True, "data": {"items": [], "total": 3}},
        ]

        async def first(method, url, headers, db_session=None, identifier="default"):
            return pages.pop(0)

        client._make_request = first  # type: ignore[method-assign]
        result = await client.get_members("token", "acc", None)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "incomplete")

        async def repeats(method, url, headers, db_session=None, identifier="default"):
            return {"success": True, "data": {"items": [{"email": "a@x.com", "id": "user-a"}], "total": 4}}

        client._make_request = repeats  # type: ignore[method-assign]
        repeated = await client.get_members("token", "acc", None)
        self.assertFalse(repeated["success"])
        self.assertEqual(repeated["error_code"], "incomplete")

        calls = {"n": 0}

        async def many(method, url, headers, db_session=None, identifier="default"):
            calls["n"] += 1
            return {
                "success": True,
                "data": {
                    "items": [{"email": f"u{calls['n']}@x.com", "id": f"user-{calls['n']}"} for _ in range(50)],
                    "total": 5000,
                },
            }

        client._make_request = many  # type: ignore[method-assign]
        capped = await client.get_members("token", "acc", None)
        self.assertFalse(capped["success"])
        self.assertLessEqual(calls["n"], ChatGPTClient.MAX_PAGES)


class WorkspaceQueryAndSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        self.workspace, _ = await upsert_workspace(
            self.session,
            source_team_id=1,
            official_workspace_id="ws-1",
            name="Team One",
            subscription_plan=None,
            owner_account_id=mother.id,
            status="active",
            seat_limit=None,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_nested_sync_keeps_owner_out_of_remote_only_and_query_shows_james(self):
        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [
                        {"email": "owner@example.com", "role": "account-owner", "id": "u-owner"},
                        NESTED_JAMES,
                    ],
                    "total": 2,
                    "reported_total": 2,
                    "raw_item_count": 2,
                },
                invites={"success": True, "items": [], "total": 0},
            )
        )
        result = await service.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["joined"], 2)
        self.assertEqual(result["remote_only"], 1)
        self.assertIn("官方已加入 2", result["message"])
        payload = await workspaces_query(self.session)
        item = payload["items"][0]
        self.assertEqual(item["members"], 1)
        emails = {row["email"] for row in item["official_members"]}
        self.assertIn("james.smith@example.com", emails)
        james = next(row for row in item["reconciliation"]["items"] if row["email"] == "james.smith@example.com")
        self.assertEqual(james["status"], "remote_only")
        self.assertEqual(item["managed"]["count"], 0)

    async def test_schema_mismatch_keeps_old_snapshot(self):
        first = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [NESTED_JAMES],
                    "total": 1,
                    "reported_total": 1,
                    "raw_item_count": 1,
                }
            )
        )
        ok = await first.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(ok["ok"])
        before = (await self.session.get(Workspace, self.workspace.id)).last_official_sync_at
        count_before = len(list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars()))
        failing = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [{"id": "user-x", "profile": {"name": "nope"}}],
                    "total": 2,
                    "reported_total": 2,
                    "raw_item_count": 1,
                }
            )
        )
        second = await failing.sync_workspace(self.session, self.workspace.id)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "schema_mismatch")
        after = (await self.session.get(Workspace, self.workspace.id)).last_official_sync_at
        count_after = len(list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars()))
        self.assertEqual(before, after)
        self.assertEqual(count_before, count_after)

    async def test_accounts_query_returns_workspace_id(self):
        payload = await accounts_query(self.session)
        owner = payload["items"][0]
        self.assertEqual(owner["workspace_id"], self.workspace.id)
        self.assertEqual(owner["primary_workspace_id"], self.workspace.id)
        self.assertIsInstance(owner["memberships"], list)


class LookupAndKickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        self.workspace, _ = await upsert_workspace(
            self.session,
            source_team_id=9,
            official_workspace_id="ws-9",
            name="Team",
            subscription_plan=None,
            owner_account_id=mother.id,
            status="active",
            seat_limit=None,
        )
        child = Account(email="kid@example.com", local_purpose="child", operational_state="active", official_plan="unknown", auth_state="unknown")
        self.session.add(child)
        await self.session.flush()
        self.child = child
        binding = ExternalBinding(provider=PROVIDER_SUB2API, local_account_id=child.id, remote_account_id="55", binding_state="verified")
        self.session.add(binding)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_unknown_lookup_blocks_kick_and_local_changes(self):
        service = WorkspaceService()
        service.get_members = AsyncMock(return_value={"success": False, "error": "timeout", "error_code": "transport", "members": []})
        service.get_invites = AsyncMock(return_value={"success": True, "items": [], "total": 0})
        live, found = await service.lookup_live_member(self.session, self.workspace, "kid@example.com")
        self.assertIsNone(found)
        self.assertEqual(live["lookup_state"], "unknown_due_to_error")
        from app.application.rotate import RotateService

        rotate = RotateService(workspaces=service, sub2api=AsyncMock())
        result = await rotate.kick_to_standby(self.session, workspace_id=self.workspace.id, email="kid@example.com", unbind_sub2api=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "manual_required")
        child = await self.session.get(Account, self.child.id)
        self.assertEqual(child.operational_state, "active")
        binding = (await self.session.execute(select(ExternalBinding))).scalar_one()
        self.assertEqual(binding.binding_state, "verified")

    async def test_sub2api_delete_failure_keeps_binding_partial(self):
        from app.application.rotate import RotateService

        class _WS:
            async def load_workspace(self, db, workspace_id):
                return await db.get(Workspace, workspace_id)

            async def lookup_live_member(self, db, workspace, email):
                self._lookups = getattr(self, "_lookups", 0) + 1
                if self._lookups == 1:
                    return {"success": True, "lookup_state": "found"}, {"email": email, "status": "joined", "user_id": "user-kid"}
                return {"success": True, "lookup_state": "absent_confirmed"}, None

            async def delete_member(self, db, workspace_id, user_id, email=None):
                return {"success": True, "message": "kicked"}

            client = type("C", (), {"pick_user_id": staticmethod(lambda item: item.get("user_id"))})()

            async def mark_standby(self, db, account, next_eligible_at=None, unbind_sub2api=False, remote_unbind_confirmed=False, binding_error=None):
                from app.application.workspaces import workspace_service

                await workspace_service.mark_standby(
                    db,
                    account,
                    next_eligible_at=next_eligible_at,
                    unbind_sub2api=unbind_sub2api,
                    remote_unbind_confirmed=remote_unbind_confirmed,
                    binding_error=binding_error,
                )

        sub = AsyncMock()
        sub.delete_accounts = AsyncMock(side_effect=RuntimeError("sub down"))
        rotate = RotateService(workspaces=_WS(), sub2api=sub)
        rotate._remote_id_for = AsyncMock(return_value="55")
        result = await rotate.kick_to_standby(self.session, workspace_id=self.workspace.id, email="kid@example.com", unbind_sub2api=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "partial")
        binding = (await self.session.execute(select(ExternalBinding))).scalar_one()
        self.assertEqual(binding.binding_state, "error")


class ConsoleLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        self.workspace, _ = await upsert_workspace(
            self.session,
            source_team_id=2,
            official_workspace_id="ws-2",
            name="Team",
            subscription_plan=None,
            owner_account_id=self.mother.id,
            status="active",
            seat_limit=None,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_manual_rotate_goes_through_saga(self):
        with patch("app.application.console_actions.rotate_service.run_rotate_saga", new=AsyncMock(return_value={"success": True, "status": "success"})) as saga:
            result = await start_controlled_rotate(self.session, self.workspace.id, email="kid@example.com", reason="console")
        self.assertTrue(result["ok"])
        saga.assert_awaited()
        kwargs = saga.await_args.kwargs
        self.assertEqual(kwargs["email"], "kid@example.com")
        self.assertTrue(kwargs["skip_confirm"])

    async def test_partial_refresh_is_not_success(self):
        with (
            patch("app.application.console_actions.auth_service.refresh_account", new=AsyncMock(return_value={"success": True})),
            patch("app.application.console_actions.quota_service.probe_account", new=AsyncMock(side_effect=RuntimeError("quota down"))),
            patch("app.application.console_actions.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.console_actions.verify_bindings", new=AsyncMock(return_value={"bindings": []})),
        ):
            result = await account_refresh(self.session, self.mother.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["partial"])

    async def test_account_sub2api_sync_is_account_scoped(self):
        with (
            patch("app.application.console_actions.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[{"id": 1}])),
            patch(
                "app.application.console_actions.verify_bindings",
                new=AsyncMock(return_value={"bindings": [{"local_account_id": self.mother.id, "binding_state": "verified"}]}),
            ),
        ):
            result = await account_sub2api_sync(self.session, self.mother.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], self.mother.id)
        self.assertTrue(result["matched"])

    async def test_retry_dispatches_typed_handler(self):
        row = await operation_store.create(self.session, op_type="quota_probe", account_id=self.mother.id, email=self.mother.email)
        await operation_store.finish(self.session, row, {"success": False, "error": "boom"})
        await self.session.commit()
        with patch("app.application.console_actions.account_quota_probe", new=AsyncMock(return_value={"ok": True, "operation_id": "abc"})) as probe:
            result = await retry_operation(self.session, row.public_id)
        self.assertTrue(result["ok"])
        probe.assert_awaited()

    async def test_cancel_is_checked_by_handler(self):
        from app.application.rotate import RotateService

        op = await operation_store.create(self.session, op_type="kick_member", workspace_id=self.workspace.id, email="kid@example.com", state="running")
        op.cancel_requested = True
        await self.session.commit()
        rotate = RotateService(workspaces=_FakeWorkspaces())
        result = await rotate.kick_to_standby(self.session, workspace_id=self.workspace.id, email="kid@example.com", job_id=op.public_id)
        self.assertEqual(result["status"], "cancelled")

    async def test_two_workers_do_not_steal_unexpired_lease(self):
        future = utcnow() + timedelta(minutes=10)
        row = await operation_store.create(self.session, op_type="quota_probe", email="a@x.com")
        row.locked_by = "worker-a:1"
        row.lease_expires_at = future
        await self.session.commit()
        recovered = await operation_store.recover_stale(self.session, worker_id="worker-b:2", reclaim_all_active=False)
        self.assertEqual(recovered, [])
        stats = await recover_stale_operations(self.session)
        self.assertEqual(stats["recovered"], 0)

    async def test_proxy_update_clears_session(self):
        with patch("app.integrations.openai.chatgpt.chatgpt_client.clear_session", new=AsyncMock()) as clear:
            result = await update_account_proxy(self.session, self.mother.id, proxy="socks5://127.0.0.1:1080")
        self.assertTrue(result["ok"])
        clear.assert_awaited()

    async def test_phone_cooldown_and_get_has_no_write(self):
        now = utcnow()
        phone = PhonePool(number="+15550001111", sms_url="https://sms.example/a", status="active", used_count=1, last_used_at=now)
        self.session.add(phone)
        await self.session.commit()
        cfg = await phone_pool_service.get_config(self.session)
        payload = phone_pool_service.serialize(phone, cfg)
        self.assertNotEqual(payload["cooldown_until"], payload["last_used_at"])
        self.assertIsNotNone(payload["available_at"])
        listed = await phone_pool_service.list_phones(self.session)
        self.assertEqual(listed[0].used_count, 1)

    async def test_hme_readonly_does_not_purge(self):
        now = utcnow()
        lease = HmeAliasLease(
            email="gone@icloud.com",
            anonymous_id="a1",
            account_id="acc",
            local_state="reserved",
            expires_at=now - timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        self.session.add(lease)
        await self.session.commit()
        with patch("app.application.resources.hme.purge_expired_leases", new=AsyncMock()) as purge:
            await reconcile_aliases(self.session, aliases=[], readonly=True)
        purge.assert_not_awaited()
        occupied = await occupied_account_emails(self.session)
        self.assertIn("owner@example.com", occupied)

    async def test_tls_default_verifies(self):
        client = ChatGPTClient()
        self.assertTrue(client._tls_verify())

    async def test_operation_pack_has_no_plaintext_secrets(self):
        packed = pack_input(
            {
                "password": "secret-pass",
                "proxy": "socks5://user:hunter2@127.0.0.1:1080",
                "phone_line": "+1----https://sms.example/key=abc",
                "email_line": "a@x.com----https://pickup.example/secret",
            }
        )
        self.assertNotIn("secret-pass", packed)
        self.assertNotIn("hunter2", packed)
        self.assertNotIn("pickup.example/secret", packed)


class UIActionMatrixTests(unittest.TestCase):
    def test_success_sync_does_not_auto_open_operation_and_toast_has_counts(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            js = client.get("/static/js/app.js").text
            self.assertIn("handleActionResult", js)
            self.assertIn("syncToastMessage", js)
            self.assertIn("官方已加入", js)
            self.assertIn("尚未纳入本地管理", js)
            self.assertNotIn("还没有子号", js)
            self.assertLess(js.count("if (result.operation_id) await openOperationById(result.operation_id)"), 3)
            self.assertIn("can_retry", js)
            self.assertIn("chooseWorkspaceId", js)
            self.assertIn("chooseWorkspaceId", js)
            html = client.get("/workspaces").text
            self.assertIn("席位", html)
