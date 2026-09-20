"""401 rotation uses a fresh official response, never a stale remote error string."""
import unittest
from unittest.mock import AsyncMock

from app.application.operations import operation_store
from app.application.rotate import RotateService
from app.domain.quota import QuotaResult
from app.domain.rotate import should_unbind_sub2api
from tests import test_automatic_rotation as fixtures
from tests.test_rotate import _FakeSub2Api


class UnauthorizedRotationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AutomaticRotationTests.asyncSetUp
    asyncTearDown = fixtures.AutomaticRotationTests.asyncTearDown
    seed = fixtures.AutomaticRotationTests.seed

    def response(self, **overrides):
        return QuotaResult(**dict(dict(success=False, http_status=401, error_code="unauthorized",
                                      credential_revision=1), **overrides))

    async def service(self, result, *, remote_error="HTTP 401 unauthorized", snapshots=None):
        child, workspace, remote = await self.seed()
        child.last_reauth_code = None
        await self.db.commit()
        remote["error_message"] = remote_error
        quota = AsyncMock()
        quota.probe_account.return_value = result
        quota.latest_official_by_contexts.return_value = snapshots or {}
        service = RotateService(sub2api=_FakeSub2Api([remote]), quota=quota)
        service.run_rotate_saga = AsyncMock(return_value={"success": True})
        return service, child, workspace

    async def test_confirmed_401_enters_existing_rotation_and_unbinds_old_sub(self):
        service, child, workspace = await self.service(self.response())
        stats = await service.run_once(self.db, settings=self.cfg, in_test=True)
        self.assertEqual(stats["rotated"], 1)
        self.assertEqual(service.run_rotate_saga.await_args.kwargs["reason"], "unauthorized")
        service.quota.probe_account.assert_awaited_once_with(self.db, child, workspace_id=workspace.id)
        self.assertTrue(should_unbind_sub2api("unauthorized"))

    async def test_recovered_authorization_does_not_rotate_on_remote_401(self):
        service, _, _ = await self.service(self.response(success=True, http_status=200, error_code=None))
        stats = await service.run_once(self.db, settings=self.cfg, in_test=True)
        self.assertEqual(stats["rotated"], 0)
        service.run_rotate_saga.assert_not_awaited()

    async def test_ambiguous_or_old_probe_results_cannot_confirm_401(self):
        child, workspace, _ = await self.seed()
        quota = AsyncMock()
        service = RotateService(quota=quota)
        for change in ({"http_status": 403}, {"http_status": None, "error_code": "transport"},
                       {"error_code": "superseded"}, {"credential_revision": 2},
                       {"credential_revision": None}, {"source": "sub2api"},
                       {"error_source": "proxy"}, {"success": True}):
            with self.subTest(change=change):
                quota.probe_account.return_value = self.response(**change)
                result = await service._confirm_unauthorized(self.db, account=child, workspace_id=workspace.id)
                self.assertFalse(result["ok"])
        quota.probe_account.side_effect = TimeoutError()
        result = await service._confirm_unauthorized(self.db, account=child, workspace_id=workspace.id)
        self.assertFalse(result["ok"])

    async def test_local_official_401_is_candidate_even_when_sub_status_has_not_caught_up(self):
        service, child, workspace = await self.service(self.response(), remote_error="")
        service.quota.latest_official_by_contexts.return_value = {(child.id, workspace.id): self.response()}
        stats = await service.run_once(self.db, settings=self.cfg, in_test=True)
        self.assertEqual(stats["rotated"], 1)
        self.assertEqual(stats["reason"], "unauthorized")

    async def test_old_revision_snapshot_does_not_enter_queue(self):
        service, child, workspace = await self.service(self.response(), remote_error="")
        service.quota.latest_official_by_contexts.return_value = {
            (child.id, workspace.id): self.response(credential_revision=0),
        }
        await service.run_once(self.db, settings=self.cfg, in_test=True)
        service.quota.probe_account.assert_not_awaited()
        service.run_rotate_saga.assert_not_awaited()

    async def test_401_keeps_daily_limit(self):
        service, _, _ = await self.service(self.response())
        stats = await service.run_once(self.db, settings={**self.cfg, "auto_rotate_daily_limit": 0}, in_test=True)
        self.assertEqual(stats["capped"], 1)
        service.quota.probe_account.assert_not_awaited()
        service.run_rotate_saga.assert_not_awaited()

    async def test_old_successful_confirmation_does_not_bypass_recheck_on_resume(self):
        service, child, workspace = await self.service(self.response(success=True, http_status=200))
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=workspace.id, account_id=child.id)
        await operation_store.mark_step(self.db, op, "confirm_trigger", state="success")
        await self.db.commit()
        service._pause_and_drain = AsyncMock()
        service.kick_and_refill = AsyncMock()
        result = await RotateService.run_rotate_saga(service, self.db, job_id=op.public_id,
            workspace_id=workspace.id, email=child.email, reason="unauthorized", in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "unauthorized_not_confirmed")
        service._pause_and_drain.assert_not_awaited()
        service.kick_and_refill.assert_not_awaited()

    async def test_direct_saga_confirms_401_before_pause_and_refill(self):
        service, child, workspace = await self.service(self.response())
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=workspace.id, account_id=child.id)
        await self.db.commit()
        service._pause_and_drain = AsyncMock(return_value={"ok": True})
        service.kick_and_refill = AsyncMock(return_value={"success": True})
        result = await RotateService.run_rotate_saga(service, self.db, job_id=op.public_id,
            workspace_id=workspace.id, email=child.email, reason="unauthorized", in_test=True)
        self.assertTrue(result["success"])
        service._pause_and_drain.assert_awaited_once()
        self.assertEqual(service.kick_and_refill.await_args.kwargs["reason"], "unauthorized")
