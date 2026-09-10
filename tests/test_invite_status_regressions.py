import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.sub2api_publish import account_sub2api_reconcile, account_sub2api_push
from app.application.tokens import encrypt_secret
from app.core.time import utcnow
from app.domain.quota_health import present_context
from app.integrations.sub2api.client import Sub2ApiClient
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, WorkspaceOfficialMemberSnapshot


class CredentialHealthRegressionTests(unittest.TestCase):
    def test_quota_success_does_not_prove_refreshable_credentials(self):
        account = SimpleNamespace(credential_revision=1, access_token_encrypted="at", refresh_token_encrypted=None,
                                  auth_state="healthy", operational_state="active")
        latest = SimpleNamespace(credential_revision=1, success=True, five_hour_used_percent=0,
                                 seven_day_used_percent=0, http_status=200, check_id="check", error_source=None,
                                 error_code=None, queried_at=utcnow())
        result = present_context(account, latest=latest)
        self.assertTrue(result["needs_auth"])
        self.assertEqual(result["health"]["code"], "auth_required")
        self.assertEqual(result["latest_check"]["state"], "healthy")
        account.refresh_token_encrypted = "rt"
        account.auth_state = "manual_required"
        self.assertTrue(present_context(account, latest=latest)["needs_auth"])
        account.auth_state = "healthy"
        self.assertFalse(present_context(account, latest=latest)["needs_auth"])


class PaginationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_beyond_eight_pages(self):
        async def get(path, *, headers, params):
            page = params["page"]
            start = (page - 1) * 100
            items = [{"id": i + 1} for i in range(start, min(start + 100, 901))]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "total": 901}}, request=httpx.Request("GET", "https://example.test"))
        http = AsyncMock(get=AsyncMock(side_effect=get))
        rows = await Sub2ApiClient()._paginate_admin(http, {}, "/accounts")
        self.assertEqual(len(rows), 901)
        self.assertEqual(http.get.await_count, 10)

    async def test_incomplete_list_is_not_used_as_authoritative_absence(self):
        http = AsyncMock()
        http.get.return_value = httpx.Response(200, json={"code": 0, "data": {"items": [], "total": 901}}, request=httpx.Request("GET", "https://example.test"))
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            await Sub2ApiClient()._paginate_admin(http, {}, "/accounts")

    async def test_repeated_page_fails_closed(self):
        http = AsyncMock()
        http.get.return_value = httpx.Response(200, json={"code": 0, "data": {"items": [{"id": 1}], "total": 2}}, request=httpx.Request("GET", "https://example.test"))
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            await Sub2ApiClient()._paginate_admin(http, {}, "/accounts")


class BindingRecoveryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = Account(email="member@example.test", local_purpose="child", operational_state="active",
                               access_token_encrypted=encrypt_secret("at"), official_account_id="account-id")
        self.db.add(self.account)
        await self.db.flush()
        self.binding = ExternalBinding(provider="sub2api", local_account_id=self.account.id,
                                       remote_account_id="901", binding_state="missing")
        self.db.add(self.binding)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_missing_refresh_token_blocks_push_without_external_writes(self):
        with patch("app.application.sub2api_publish.sub2api_client.create_account", new=AsyncMock()) as create:
            result = await account_sub2api_push(self.db, self.account.id)
        self.assertEqual(result["error_code"], "missing_refresh_token")
        create.assert_not_awaited()

    async def test_missing_list_item_is_rechecked_by_bound_id(self):
        detail = {"id": 901, "credentials": {"email": self.account.email, "chatgpt_account_id": "account-id"}}
        with (
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.sub2api_publish.sub2api_client.get_account", new=AsyncMock(return_value=detail)) as get,
        ):
            result = await account_sub2api_reconcile(self.db, self.account.id)
        get.assert_awaited_once_with(self.db, 901)
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(self.binding.remote_account_id, "901")

    async def test_real_missing_binding_is_not_reported_as_never_pushed(self):
        response = httpx.Response(404, request=httpx.Request("GET", "https://example.test/accounts/901"))
        with (
            patch("app.application.sub2api_publish.sub2api_client.list_status_accounts", new=AsyncMock(return_value=[])),
            patch("app.application.sub2api_publish.sub2api_client.get_account", new=AsyncMock(side_effect=httpx.HTTPStatusError("not found", request=response.request, response=response))),
        ):
            result = await account_sub2api_reconcile(self.db, self.account.id)
        self.assertEqual(result["outcome"], "binding_remote_missing")
        self.assertFalse(result["ok"])
        self.assertEqual(self.binding.remote_account_id, "901")

    async def test_join_confirmation_updates_snapshot(self):
        from app.application.onboard import OnboardService
        from app.persistence.models.identity import Workspace
        workspace = Workspace(status="active", owner_account_id=self.account.id)
        self.db.add(workspace)
        await self.db.flush()
        workspaces = MagicMock()
        workspaces.get_members = AsyncMock(return_value={"success": True, "members": [
            {"email": self.account.email, "role": "account-owner", "seat_type": "prolite", "id": "user-1"},
        ]})
        service = OnboardService(workspaces=workspaces)
        self.assertIsNotNone(await service._confirm_joined(self.db, workspace, self.account.email))
        row = await self.db.scalar(select(WorkspaceOfficialMemberSnapshot))
        self.assertEqual(row.normalized_email, self.account.email)
        self.assertEqual(row.remote_state, "joined")
        self.assertEqual(row.seat_type, "prolite")
        await service._confirm_joined(self.db, workspace, self.account.email)
        self.assertEqual(len(list(await self.db.scalars(select(WorkspaceOfficialMemberSnapshot)))), 1)

    async def test_new_join_is_not_misreported_absent_by_old_snapshot(self):
        from datetime import timedelta
        from app.application.queries.identity import workspaces_query
        from app.persistence.models.identity import Workspace, WorkspaceMembership
        owner = Account(email="owner@example.test", local_purpose="mother", operational_state="active")
        self.db.add(owner)
        await self.db.flush()
        workspace = Workspace(status="active", owner_account_id=owner.id, last_official_sync_at=utcnow() - timedelta(days=1))
        self.db.add(workspace)
        await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=workspace.id, account_id=self.account.id,
                                       membership_state="joined", official_role="owner", local_purpose="child", joined_at=utcnow()))
        await self.db.commit()
        payload = await workspaces_query(self.db)
        item = next(item for item in payload["items"] if item["id"] == workspace.id)
        row = next(row for row in item["reconciliation"]["items"] if row["email"] == self.account.email)
        self.assertEqual(row["status"], "pending_sync")
        self.assertFalse(row["actionable"])


if __name__ == "__main__":
    unittest.main()
