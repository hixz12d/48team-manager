"""Proxy changes must affect the next official team request, including queued syncs."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.console_actions import update_account_proxy
from app.application.operations import operation_store
from app.application.proxy_resolution import ProxyResolutionError, RuntimeProxy
from app.application.tokens import encrypt_secret
from app.application.workspace_sync import WorkspaceSyncService
from app.application.workspaces import WorkspaceService
from app.core.proxy import mask_proxy_url
from app.integrations.openai.chatgpt import ChatGPTClient
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace


class WorkspaceProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.old_proxy = "http://old-user:old-password@proxy.example:8080"
        self.new_proxy = "http://new-user:new-password@proxy.example:8080"
        self.owner = Account(email="owner@example.com", local_purpose="mother", proxy=self.old_proxy,
                             access_token_encrypted=encrypt_secret("test-access"))
        self.other = Account(email="other@example.com", local_purpose="mother", proxy="http://other.example:8080")
        self.child = Account(email="child@example.com", local_purpose="child", proxy="http://child.example:8080")
        self.db.add_all([self.owner, self.other, self.child])
        await self.db.flush()
        self.workspace = Workspace(owner_account_id=self.owner.id, official_workspace_id="00000000-0000-0000-0000-000000000001")
        self.db.add(self.workspace)
        await self.db.commit()
        self.client = ChatGPTClient()
        self.transports = []

        def transport(**kwargs):
            response = SimpleNamespace(status_code=200, json=lambda: {"items": [], "total": 0})
            session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock(), config=kwargs)
            self.transports.append(session)
            return session

        factory = patch("app.integrations.openai.chatgpt.AsyncSession", side_effect=transport)
        factory.start()
        self.addCleanup(factory.stop)

    async def asyncTearDown(self):
        for identifier in list(self.client._sessions):
            await self.client.clear_session(identifier)
        await self.db.close()
        await self.engine.dispose()

    async def test_same_host_credential_changes_rebuild_session_without_leaking_secrets(self):
        previous = await self.client._get_session(self.db, self.owner.email)
        other = await self.client._get_session(self.db, self.other.email)
        for proxy in ("http://old-user:new-password@proxy.example:8080", self.new_proxy, None):
            with self.subTest(proxy=mask_proxy_url(proxy)):
                self.owner.proxy = proxy
                await self.db.commit()
                current = await self.client._get_session(self.db, self.owner.email)
                self.assertIsNot(previous, current)
                previous.close.assert_awaited_once()
                self.assertEqual(current.config["proxies"], {"all": proxy, "http": proxy, "https": proxy} if proxy else None)
                self.assertIs(current, await self.client._get_session(self.db, self.owner.email))
                self.assertIs(other, await self.client._get_session(self.db, self.other.email))
                self.assertNotIn("password", self.client._session_keys[self.owner.email])
                self.assertNotIn("user", self.client._session_keys[self.owner.email])
                previous = current
        other.close.assert_not_awaited()

    async def test_equivalent_socks_urls_reuse_session(self):
        self.owner.proxy = "socks5://proxy.example:1080"
        first = await self.client._get_session(self.db, self.owner.email)
        self.owner.proxy = "socks5h://proxy.example:1080"
        self.assertIs(first, await self.client._get_session(self.db, self.owner.email))
        first.close.assert_not_awaited()

    async def test_switch_then_sync_uses_saved_proxy_for_members_and_invites(self):
        service = WorkspaceSyncService(WorkspaceService(self.client))
        old = await self.client._get_session(self.db, self.owner.email)
        resolved = RuntimeProxy("sub2api", 12, "test-instance", self.new_proxy)
        with patch("app.application.console_actions.resolve_sub2api_proxy", AsyncMock(return_value=resolved)):
            result = await update_account_proxy(self.db, self.owner.id, proxy_selection={"source": "sub2api", "remote_id": 12})
        self.assertTrue(result["ok"])
        self.assertEqual(result["sub2api_proxy_id"], 12)
        self.assertNotIn("new-password", str(result))
        self.assertEqual(self.other.proxy, "http://other.example:8080")
        self.assertEqual(self.child.proxy, "http://child.example:8080")
        # Both direct service calls and the persistent runner's read-only branch use the owner.
        for queued in (False, True):
            with self.subTest(queued=queued):
                operation = await operation_store.create(self.db, op_type="workspace_sync", workspace_id=self.workspace.id) if queued else None
                with patch("app.application.workspace_metadata.workspace_metadata_resolver.refresh", AsyncMock(return_value={"ok": True, "found": True})):
                    result = await service.sync_workspace(self.db, self.workspace.id, operation=operation)
                self.assertTrue(result["ok"])
                session = self.client._sessions[self.owner.email]
                self.assertEqual(session.config["proxies"]["all"], self.new_proxy)
                paths = [call.args[0] for call in session.get.await_args_list]
                self.assertTrue(any("/users?" in path for path in paths), paths)
                self.assertTrue(any("/invites?" in path for path in paths), paths)
        old.close.assert_awaited_once()
        self.assertEqual(self.owner.proxy_source, "sub2api")
        self.assertEqual(self.owner.proxy_instance_key, "test-instance")
        self.assertEqual(self.owner.sub2api_proxy_id, 12)
        cleared = await update_account_proxy(self.db, self.owner.id, clear=True)
        self.assertTrue(cleared["ok"])
        self.assertIsNone(self.owner.proxy_profile_id)
        self.assertIsNone(self.owner.proxy_instance_key)
        self.assertIsNone(self.owner.sub2api_proxy_id)
        direct = await self.client._get_session(self.db, self.owner.email)
        self.assertIsNone(direct.config["proxies"])

    async def test_catalog_failure_preserves_existing_proxy_and_child_cannot_change(self):
        for error in (ProxyResolutionError("代理已停用", error_code="proxy_disabled"), RuntimeError("catalog down")):
            with patch("app.application.console_actions.resolve_sub2api_proxy", AsyncMock(side_effect=error)):
                result = await update_account_proxy(self.db, self.owner.id, proxy_selection={"source": "sub2api", "remote_id": 99})
            self.assertFalse(result["ok"])
            self.assertEqual(self.owner.proxy, self.old_proxy)
        result = await update_account_proxy(self.db, self.child.id, clear=True)
        self.assertEqual(result["error_code"], "not_mother")
        self.assertEqual(self.child.proxy, "http://child.example:8080")


if __name__ == "__main__":
    unittest.main()
