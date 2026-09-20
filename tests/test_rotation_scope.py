"""Workspace scopes are fail-closed and apply to replacement and sync retries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.application import automatic_rotation
from app.application.rotate import RotateService
from app.application.settings import upsert_setting
from app.domain.rotate import rotation_workspace_enabled
from tests import test_automatic_rotation as fixtures
from tests.test_rotate import _FakeSub2Api
from tests.helpers import make_client


class RotationScopeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AutomaticRotationTests.asyncSetUp
    asyncTearDown = fixtures.AutomaticRotationTests.asyncTearDown
    seed = fixtures.AutomaticRotationTests.seed
    pending = fixtures.AutomaticRotationTests.pending

    async def test_unconfigured_scope_never_authorizes_a_workspace(self):
        cfg = await RotateService().load_settings(self.db)
        self.assertEqual(cfg["auto_rotate_scope"], "selected")
        self.assertEqual(cfg["auto_rotate_workspace_ids"], [])
        self.assertFalse(rotation_workspace_enabled({**cfg, "auto_rotate_enabled": True}, 1))

    async def test_selected_workspace_rotates_and_unselected_workspace_is_skipped(self):
        first, ws1, r1 = await self.seed("1")
        second, ws2, r2 = await self.seed("2")
        service = RotateService(sub2api=_FakeSub2Api([r1, r2]))
        service.run_rotate_saga = AsyncMock(return_value={"success": True})
        cfg = {**self.cfg, "auto_rotate_scope": "selected", "auto_rotate_workspace_ids": [ws2.id]}
        result = await service.run_once(self.db, settings=cfg, in_test=True)
        self.assertEqual(result["rotated"], 1)
        self.assertEqual(service.run_rotate_saga.await_args.kwargs["workspace_id"], ws2.id)

    async def test_batch_and_all_require_master_switch(self):
        cfg = {**self.cfg, "auto_rotate_scope": "selected", "auto_rotate_workspace_ids": [1, 3]}
        self.assertTrue(rotation_workspace_enabled(cfg, 1))
        self.assertTrue(rotation_workspace_enabled(cfg, 3))
        self.assertFalse(rotation_workspace_enabled(cfg, 2))
        self.assertTrue(rotation_workspace_enabled({**cfg, "auto_rotate_scope": "all"}, 99))
        self.assertFalse(rotation_workspace_enabled({**cfg, "auto_rotate_enabled": False}, 1))
        self.assertFalse(rotation_workspace_enabled({**cfg, "auto_rotate_scope": "all", "auto_rotate_enabled": False}, 99))

    async def test_pending_sync_obeys_scope_and_off_switch(self):
        _, ws, _ = await self.pending()
        for cfg in ({**self.cfg, "auto_rotate_enabled": False},
                    {**self.cfg, "auto_rotate_scope": "selected", "auto_rotate_workspace_ids": [ws.id + 1]}):
            with patch.object(automatic_rotation, "publish_replacement", new=AsyncMock()) as publish:
                result = await automatic_rotation.retry_pending_publish(self.db, now=self.now, settings=cfg)
                self.assertFalse(result["retried"])
                publish.assert_not_awaited()
        with patch.object(automatic_rotation, "publish_replacement", new=AsyncMock(return_value={"success": True})) as publish:
            result = await automatic_rotation.retry_pending_publish(self.db, now=self.now,
                settings={**self.cfg, "auto_rotate_scope": "selected", "auto_rotate_workspace_ids": [ws.id]})
            self.assertTrue(result["retried"])
            publish.assert_awaited_once()

    async def test_scope_disabled_during_preflight_prevents_kick(self):
        _, _, remote = await self.seed()
        await upsert_setting(self.db, "auto_rotate_enabled", "true")
        await upsert_setting(self.db, "auto_rotate_scope", "all")
        await self.db.commit()
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.run_rotate_saga = AsyncMock()
        async def preflight(*args):
            await upsert_setting(self.db, "auto_rotate_enabled", "false")
            await self.db.commit()
            return {"role": "owner", "seat_intent": "standard"}
        with patch.object(automatic_rotation, "preflight", new=preflight), \
             patch.object(automatic_rotation, "refresh_after_rotation", new=AsyncMock()):
            await service.run_once(self.db)
        service.run_rotate_saga.assert_not_awaited()

    async def test_malformed_scope_storage_fails_closed(self):
        for value in ('null', '{}', '"all"', '[true,"1",-1]', 'broken'):
            await upsert_setting(self.db, "auto_rotate_enabled", "true")
            await upsert_setting(self.db, "auto_rotate_workspace_ids", value)
            cfg = await RotateService().load_settings(self.db)
            self.assertFalse(rotation_workspace_enabled(cfg, 1))


class RotationScopeApiTests(unittest.TestCase):
    def test_scope_validation_default_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            cfg = client.get("/api/settings").json()["automation"]["auto_rotate"]
            self.assertFalse(cfg["auto_rotate_enabled"])
            self.assertEqual(cfg["auto_rotate_scope"], "selected")
            self.assertEqual(cfg["auto_rotate_workspace_ids"], [])
            result = client.patch("/api/settings", json={"automation": {"auto_rotate": True}})
            self.assertEqual(result.status_code, 400)
            self.assertFalse(client.get("/api/runtime/status").json()["auto_rotation"]["enabled"])
            self.assertEqual(client.patch("/api/settings", json={"automation": {"auto_rotate_workspace_ids": [999]}}).status_code, 400)
            self.assertEqual(client.patch("/api/settings", json={"automation": {"auto_rotate_scope": "invalid"}}).status_code, 422)
            result = client.patch("/api/settings", json={"automation": {"auto_rotate_scope": "all", "auto_rotate": True}})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get("/api/runtime/status").json()["auto_rotation"]["scope"], "all")
            result = client.patch("/api/settings", json={"automation": {"auto_rotate_scope": "selected", "auto_rotate": False, "auto_rotate_workspace_ids": []}})
            self.assertEqual(result.status_code, 200)
            cfg = client.get("/api/runtime/status").json()["auto_rotation"]
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["scope"], "selected")
            self.assertEqual(cfg["workspace_ids"], [])
