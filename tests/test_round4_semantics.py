"""Round 4: workspace counts, naming, proxy names, Sub2API split, operations archive."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import ensure_membership, upsert_mother_account, upsert_workspace
from app.application.operations import operation_store
from app.application.resources.proxies import proxy_profile_service
from app.application.sub2api_publish import account_sub2api_push, account_sub2api_reconcile, sub2api_publish_eligibility
from app.application.workspace_sync import WorkspaceSyncService
from app.application.queries.portfolio import portfolio_query
from app.domain.identity import MEMBERSHIP_STATE_JOINED, LOCAL_PURPOSE_MOTHER, OFFICIAL_ROLE_OWNER
from app.domain.resources.proxy_names import default_name_for_account, is_legacy_auto_name, name_for_bindings
from app.domain.workspaces.names import apply_custom_name, apply_official_name, is_placeholder_or_email_name, resolve_display_name
from app.persistence.database import Base
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceOfficialMemberSnapshot
from app.persistence.models.operations import Operation
from app.persistence.models.resources import ProxyProfile
from tests.helpers import make_client


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


class WorkspaceSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
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

    async def test_portfolio_falls_back_to_owner_account_without_membership_row(self):
        payload = await portfolio_query(self.session)
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertIsNotNone(group["mother"])
        self.assertEqual(group["mother"]["email"], "owner@example.com")
        self.assertEqual(group["mother"]["kind"], "mother")

        await ensure_membership(
            self.session,
            workspace_id=self.workspace.id,
            account_id=group["mother"]["id"],
            official_role=OFFICIAL_ROLE_OWNER,
            membership_state=MEMBERSHIP_STATE_JOINED,
            local_purpose=LOCAL_PURPOSE_MOTHER,
        )
        await self.session.commit()
        again = await portfolio_query(self.session)
        self.assertEqual(again["groups"][0]["mother"]["email"], "owner@example.com")
        self.assertEqual(again["groups"][0]["counts"]["current_children"], 0)

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_owner_plus_member_counts_and_remote_only_label(self):
        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [
                        {"email": "owner@example.com", "role": "account-owner", "id": "u-owner", "name": "Bunqor"},
                        {"email": "james@example.com", "role": "standard-user", "id": "u-james", "name": "James Smith"},
                    ],
                    "total": 2,
                }
            )
        )
        result = await service.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["joined_people_total"], 2)
        self.assertEqual(result["owner_count"], 1)
        self.assertEqual(result["joined_member_count"], 1)
        self.assertEqual(result["remote_only"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            # query API via app DB is separate; assert local query helper semantics via service result + snapshots
            snaps = list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars())
            self.assertEqual(len(snaps), 2)

    async def test_workspace_name_never_falls_back_to_email(self):
        workspace = Workspace(official_workspace_id="abc-def-12345678", name=None)
        apply_official_name(workspace, None, owner_email="owner@example.com")
        display = resolve_display_name(workspace, owner_email="owner@example.com")
        self.assertFalse(is_placeholder_or_email_name(display["display_name"]) is False and "@" in display["display_name"])
        self.assertIn("未命名工作区", display["display_name"])
        self.assertNotIn("@", display["display_name"])
        apply_custom_name(workspace, "运营一组")
        display = resolve_display_name(workspace)
        self.assertEqual(display["display_name"], "运营一组")
        apply_official_name(workspace, "", owner_email="owner@example.com")
        self.assertEqual(resolve_display_name(workspace)["display_name"], "运营一组")


class ProxyNamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_mother_proxy_auto_name_not_mother_id(self):
        account = Account(
            email="hixz262@gmail.com",
            official_plan="unknown",
            auth_state="unknown",
            operational_state="active",
            local_purpose="mother",
        )
        self.session.add(account)
        await self.session.flush()
        profile = await proxy_profile_service.upsert_from_url(
            self.session,
            "socks5h://user:pass@127.0.0.1:1080",
            name=default_name_for_account(purpose="mother", email=account.email),
            name_source="auto",
            bound_account=account,
        )
        self.assertFalse(is_legacy_auto_name(profile.name))
        self.assertEqual(profile.name, "母号 · hixz262@gmail.com")
        self.assertNotRegex(profile.name or "", r"^mother-\d+$")

    def test_legacy_and_shared_names(self):
        self.assertTrue(is_legacy_auto_name("mother-2"))
        self.assertEqual(
            name_for_bindings(host="1.1.1.1", port=1080, bindings=[{"email": "a@x.com", "purpose": "child"}]),
            "子号 · a@x.com",
        )
        self.assertEqual(
            name_for_bindings(
                host="1.1.1.1",
                port=1080,
                bindings=[{"email": "a@x.com", "purpose": "mother"}, {"email": "b@x.com", "purpose": "child"}],
            ),
            "共享代理 · 1.1.1.1:1080",
        )


class Sub2ApiSplitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.session = self.session_maker()
        self.account = Account(
            email="newxiaozhu1@gmail.com",
            official_plan="unknown",
            auth_state="healthy",
            operational_state="active",
            local_purpose="child",
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            official_account_id="acct-1",
        )
        self.session.add(self.account)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_reconcile_remote_missing_is_warning_not_green_sync_success(self):
        with patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])):
            with patch("app.application.sub2api_publish.verify_bindings", new=AsyncMock(return_value={"bindings": []})):
                result = await account_sub2api_reconcile(self.session, self.account.id)
        self.assertEqual(result["outcome"], "remote_missing")
        self.assertEqual(result.get("binding_state"), "unbound")
        self.assertIn("尚未推送", result["message"])
        self.assertEqual(result.get("tone"), "warning")
        # no verified binding created
        bindings = list((await self.session.execute(select(ExternalBinding))).scalars())
        self.assertEqual(bindings, [])

    async def test_push_create_and_verify_binding(self):
        remote = {
            "id": 123,
            "name": "newxiaozhu1@gmail.com",
            "credentials": {"email": "newxiaozhu1@gmail.com", "chatgpt_account_id": "acct-1"},
            "extra": {"email": "newxiaozhu1@gmail.com"},
        }

        async def _create(db, payload):
            return {"id": 123, **payload}

        with (
            patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda raw: "token" if raw else ""),
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.sub2api_publish.sub2api_client.create_account", new=AsyncMock(side_effect=_create)),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=remote)),
            patch("app.application.sub2api_publish.sub2api_client.set_account_schedulable", new=AsyncMock(return_value={"patched": True})),
        ):
            result = await account_sub2api_push(self.session, self.account.id, group_ids=[1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["remote_id"], 123)
        binding = (
            await self.session.execute(select(ExternalBinding).where(ExternalBinding.local_account_id == self.account.id))
        ).scalar_one()
        self.assertEqual(binding.binding_state, "verified")
        self.assertEqual(binding.remote_account_id, "123")

    async def test_push_does_not_update_email_only_remote(self):
        other = {
            "id": 7,
            "name": "newxiaozhu1@gmail.com",
            "credentials": {"email": "newxiaozhu1@gmail.com", "chatgpt_account_id": "acct-1"},
            "extra": {"email": "newxiaozhu1@gmail.com"},
        }
        created = {
            "id": 88,
            "name": "Team（newxiaozhu1） 子号",
            "credentials": {"email": "newxiaozhu1@gmail.com", "chatgpt_account_id": "acct-1"},
            "extra": {"email": "newxiaozhu1@gmail.com", "team48_context_key": "team48::1"},
        }

        async def _create(db, payload):
            self.assertEqual(payload["extra"]["team48_context_key"], "team48::1")
            return {"id": 88, **payload}

        update = AsyncMock(side_effect=AssertionError("must not update email-only remote"))
        with (
            patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda raw: "token" if raw else ""),
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[other])),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=update),
            patch("app.application.sub2api_publish.sub2api_client.create_account", new=AsyncMock(side_effect=_create)),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=created)),
            patch("app.application.sub2api_publish.sub2api_client.set_account_schedulable", new=AsyncMock(return_value={"patched": True})),
        ):
            result = await account_sub2api_push(self.session, self.account.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remote_id"], 88)
        update.assert_not_awaited()

    async def test_push_preserves_binding_when_remote_read_fails(self):
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=self.account.id,
                remote_account_id="2944",
                binding_state="verified",
            )
        )
        await self.session.commit()

        class _MissingRemote(Exception):
            def __init__(self):
                super().__init__("Client error '404 Not Found' for url 'http://sub2api-canary:8080/api/v1/admin/accounts/2944'")
                self.response = type("Resp", (), {"status_code": 404})()

        created = {
            "id": 4001,
            "name": "Team（newxiaozhu1） 子号",
            "credentials": {"email": "newxiaozhu1@gmail.com", "chatgpt_account_id": "acct-1"},
            "extra": {"email": "newxiaozhu1@gmail.com"},
        }
        update = AsyncMock(side_effect=_MissingRemote())
        create = AsyncMock(return_value={"id": 4001})
        with (
            patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda raw: "token" if raw else ""),
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.sub2api_publish.sub2api_client.get_account", new=AsyncMock(side_effect=_MissingRemote())),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=update),
            patch("app.application.sub2api_publish.sub2api_client.create_account", new=create),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=created)),
            patch("app.application.sub2api_publish.sub2api_client.set_account_schedulable", new=AsyncMock(return_value={"patched": True})),
        ):
            result = await account_sub2api_push(self.session, self.account.id)
        self.assertFalse(result["ok"])
        create.assert_not_awaited()
        update.assert_not_awaited()
        binding = (
            await self.session.execute(select(ExternalBinding).where(ExternalBinding.local_account_id == self.account.id))
        ).scalar_one()
        self.assertEqual(binding.remote_account_id, "2944")
        self.assertEqual(binding.binding_state, "verified")

    async def test_push_write_ok_but_verify_fail_stays_partial(self):
        bad_remote = {"id": 9, "credentials": {"email": "other@example.com"}}
        with (
            patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda raw: "token" if raw else ""),
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.sub2api_publish.sub2api_client.create_account", new=AsyncMock(return_value={"id": 9})),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=bad_remote)),
        ):
            result = await account_sub2api_push(self.session, self.account.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "verification_failed")
        self.assertIn(result["status"], {"manual_required", "partial"})
        binding = (
            await self.session.execute(select(ExternalBinding).where(ExternalBinding.local_account_id == self.account.id))
        ).scalar_one_or_none()
        if binding is not None:
            self.assertNotEqual(binding.binding_state, "verified")

    def test_eligibility_requires_token(self):
        bare = Account(
            email="x@y.com",
            official_plan="unknown",
            auth_state="unknown",
            operational_state="active",
            local_purpose="child",
        )
        payload = sub2api_publish_eligibility(bare)
        self.assertFalse(payload["eligible"])
        self.assertIn("token", payload["missing_fields"])


class OperationsCenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_filter_paginate_and_soft_archive(self):
        ok = await operation_store.create(self.session, op_type="quota_probe", email="a@x.com", source="manual")
        await operation_store.finish(self.session, ok, {"success": True, "status": "success", "message": "done"})
        bad = await operation_store.create(self.session, op_type="sub2api_reconcile", email="b@x.com", source="manual")
        await operation_store.finish(
            self.session,
            bad,
            {"success": True, "status": "success", "outcome": "remote_missing", "message": "missing"},
        )
        running = await operation_store.create(self.session, op_type="workspace_sync", email="c@x.com")
        await self.session.commit()

        filtered = await operation_store.list_filtered(self.session, state="success", page=1, page_size=10)
        self.assertEqual(filtered["total"], 2)
        self.assertTrue(all(row.state == "success" for row in filtered["items"]))

        from app.application.console_maintenance import archive_operation, bulk_archive_operations, restore_operation

        blocked = await archive_operation(self.session, running.public_id)
        self.assertFalse(blocked["ok"])
        archived = await archive_operation(self.session, ok.public_id, reason="test")
        self.assertTrue(archived["ok"])
        hidden = await operation_store.list_filtered(self.session, page=1, page_size=50)
        self.assertEqual(hidden["total"], 2)  # bad + running
        shown = await operation_store.list_filtered(self.session, archived_only=True, page=1, page_size=50)
        self.assertEqual(shown["total"], 1)
        restored = await restore_operation(self.session, ok.public_id)
        self.assertTrue(restored["ok"])
        bulk = await bulk_archive_operations(self.session, [ok.public_id, bad.public_id, running.public_id])
        self.assertEqual(bulk["count"], 2)
        self.assertTrue(any(item["reason"] in {"active", "state=running"} or str(item.get("reason","")).startswith("state=") for item in bulk["skipped"]))


class Round4ApiContractTests(unittest.TestCase):
    def test_api_and_ui_contracts(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            ops = client.get("/api/operations").json()
            self.assertIn("page", ops)
            self.assertIn("facets", ops)
            js = client.get("/static/js/app.js").text
            self.assertIn('id: "account.sub2api"', js)
            self.assertIn("/api/accounts/${item.id}/sub2api/push", js)
            self.assertIn("/api/accounts/${item.id}/sub2api/reconcile", js)
            self.assertIn("operations-search", client.get("/operations").text)
            self.assertIn("账号与团队", client.get("/workspaces").text)
            redirect = client.get("/workspaces?workspace=7", follow_redirects=False)
            self.assertEqual(redirect.status_code, 303)
            self.assertIn("workspace=7", redirect.headers["location"])
            # endpoints exist
            missing = client.post("/api/accounts/999/sub2api/reconcile")
            self.assertEqual(missing.status_code, 404)
            push = client.post("/api/accounts/999/sub2api/push", json={})
            self.assertEqual(push.status_code, 404)


if __name__ == "__main__":
    unittest.main()
