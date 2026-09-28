"""Manual one-for-one rotation: stage order, break points and continuation. SQLite + fakes only."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application import manual_rotation as mr
from app.application.operations import operation_store
from app.application.tokens import encrypt_secret
from app.domain.rotate import manual_rotation_email_error
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.operations import Operation, OperationStep

WS_UUID = "11111111-1111-1111-1111-111111111111"
FREE = {"is_free": True, "has_billing_notice": False, "replacement_required": False}


class FakeSub2Api:
    def __init__(self):
        self.remotes = {}
        self.calls = []
        self.delete_ok = True

    async def load_config(self, db):
        return {"configured": True, "base_url": "http://sub.test"}

    async def list_status_accounts(self, db):
        self.calls.append(("list",))
        return [dict(row) for row in self.remotes.values()]

    async def get_account(self, db, remote_id):
        self.calls.append(("get", int(remote_id)))
        remote = self.remotes.get(int(remote_id))
        if remote is None:
            request = httpx.Request("GET", f"http://sub.test/{remote_id}")
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        return dict(remote)

    async def set_account_schedulable(self, db, remote_id, value):
        self.calls.append(("pause", int(remote_id)))
        self.remotes[int(remote_id)]["schedulable"] = value
        return {"patched": True}

    async def delete_accounts(self, db, ids):
        self.calls.append(("delete", list(ids)))
        if not self.delete_ok:
            raise httpx.ReadTimeout("lost")
        for remote_id in ids:
            self.remotes.pop(int(remote_id), None)
        return {"deleted": list(ids), "failed": []}


def remote(remote_id, email, *, status="active", schedulable=True):
    return {"id": remote_id, "platform": "openai", "type": "oauth", "status": status, "schedulable": schedulable,
            "credentials": {"email": email, "workspace_id": WS_UUID}}


class ManualRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)()
        self.owner = Account(email="owner@example.com", local_purpose="mother", operational_state="active",
                             proxy="socks5h://owner.invalid:1080", access_token_encrypted=encrypt_secret("tok"))
        self.old = Account(email="old@icloud.com", local_purpose="child", operational_state="active", auth_state="healthy")
        self.db.add_all([self.owner, self.old])
        await self.db.flush()
        self.ws = Workspace(official_workspace_id=WS_UUID, owner_account_id=self.owner.id, status="active", seat_limit=5, name="North")
        self.db.add(self.ws)
        await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=self.ws.id, account_id=self.old.id, membership_state="joined",
                                        official_role="member", local_purpose="child"))
        self.old_binding = ExternalBinding(provider="sub2api", local_account_id=self.old.id, workspace_id=self.ws.id,
                                           remote_account_id="88", binding_state="verified")
        self.db.add(self.old_binding)
        await self.db.commit()

        self.sub = FakeSub2Api()
        self.sub.remotes[88] = remote(88, "old@icloud.com")
        self.members = {"old@icloud.com": {"email": "old@icloud.com", "status": "joined", "role": "standard-user",
                                           "seat_type": "prolite", "user_id": "user-old"}}
        self.kick_vacancy = FREE
        self.onboard_outcome = "authorized"
        self.onboard_calls = []
        self.publish_calls = []
        self.publish_ok = True
        self.rotate = self._rotate()
        for target, value in {
            "validate_configuration": None, "load_cf_config": AsyncMock(return_value={"a": "1", "b": "2", "c": "3"}),
            "probe_mail": AsyncMock(return_value={"ok": True}), "load_defaults": AsyncMock(),
            "validate_defaults": AsyncMock(), "refresh_after_rotation": AsyncMock(),
        }.items():
            patcher = patch.object(mr, target, new=value if value is not None else (lambda *a, **k: None))
            patcher.start()
            self.addCleanup(patcher.stop)
        publish = patch.object(mr, "publish_replacement", new=self._publish)
        publish.start()
        self.addCleanup(publish.stop)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    # -- fakes

    def _rotate(self):
        test = self

        async def lookup(db, workspace, email):
            return {"success": True}, test.members.get(email)

        async def kick(db, *, workspace_id, email, job_id, keep_remote, **kw):
            test.assertTrue(keep_remote)
            test.members.pop(email, None)
            op = await operation_store.get_by_public_id(db, job_id)
            await operation_store.mark_step(db, op, "official_removed", state="success")
            account = await db.scalar(select(Account).where(Account.email == email))
            membership = await db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id))
            membership.membership_state = "removed"
            account.operational_state = "standby"
            await db.commit()
            return {"success": True, "vacancy": test.kick_vacancy}

        async def pause(db, *, job_id, remote_id, **kw):
            await test.sub.set_account_schedulable(db, remote_id, False)
            op = await operation_store.get_by_public_id(db, job_id)
            await operation_store.mark_step(db, op, "paused", state="success")
            await db.commit()
            return {"ok": True}

        async def binding_for(db, account, *, workspace_id=None):
            row = await db.scalar(select(ExternalBinding).where(ExternalBinding.local_account_id == account.id,
                                                                 ExternalBinding.workspace_id == workspace_id))
            if row is None:
                return {"state": "absent", "remote_id": ""}
            if row.binding_state != "verified":
                return {"state": "ambiguous_or_unverified", "remote_id": "", "binding": row, "reason": "binding_not_verified"}
            return {"state": "matched", "remote_id": row.remote_account_id, "binding": row}

        async def load_workspace(db, workspace_id):
            return await db.get(Workspace, workspace_id)

        async def owner_account(db, workspace):
            return await db.get(Account, workspace.owner_account_id)

        workspaces = SimpleNamespace(
            load_workspace=load_workspace, owner_account=owner_account,
            lookup_live_member=lookup, _last_owner_guard=lambda *a, **k: None,
        )
        onboard = SimpleNamespace(invite_and_onboard=self._onboard)
        return SimpleNamespace(workspaces=workspaces, sub2api=self.sub, onboard=onboard,
                               kick_to_standby=kick, _pause_and_drain=pause, _remote_binding_for=binding_for)

    async def _onboard(self, db, **kwargs):
        self.onboard_calls.append(kwargs)
        email = kwargs["email_line"]
        account = await db.scalar(select(Account).where(Account.email == email))
        if account is None:
            account = Account(email=email, local_purpose="child", operational_state="available")
            db.add(account)
            await db.flush()
        if self.onboard_outcome == "phone":
            return {"success": False, "error_code": "phone_verification_required", "error": "需要手机验证",
                    "child": {"id": account.id}}
        membership = await db.scalar(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == self.ws.id, WorkspaceMembership.account_id == account.id))
        if membership is None:
            db.add(WorkspaceMembership(workspace_id=self.ws.id, account_id=account.id, membership_state="joined",
                                       official_role=kwargs["role"], local_purpose="child"))
        self.members[email] = {"email": email, "status": "joined", "role": kwargs["role"],
                               "seat_type": "prolite" if kwargs["seat_intent"] == "premium" else "default"}
        account.operational_state = "active"
        if self.onboard_outcome == "oauth_failed":
            return {"success": False, "partial": True, "joined": True, "authorized": False,
                    "error_code": "oauth_failed", "child": {"id": account.id}}
        account.auth_state = "healthy"
        account.refresh_token_encrypted = encrypt_secret("rt")
        account.access_token_encrypted = encrypt_secret(jwt.encode({
            "email": email, "https://api.openai.com/auth": {"chatgpt_account_id": WS_UUID},
        }, "fixture-only-key-at-least-32-bytes", algorithm="HS256"))
        account.credential_revision = 1
        return {"success": True, "joined": True, "authorized": True, "child": {"id": account.id}}

    async def _publish(self, db, invite, workspace_id):
        self.publish_calls.append(dict(invite))
        if not self.publish_ok:
            return {**invite, "success": False, "publish_pending": True, "publish_written": True}
        account_id = invite["child"]["id"]
        if await db.scalar(select(ExternalBinding).where(ExternalBinding.local_account_id == account_id)) is None:
            db.add(ExternalBinding(provider="sub2api", local_account_id=account_id, workspace_id=workspace_id,
                                   remote_account_id="99", binding_state="verified"))
            await db.commit()
        self.sub.remotes[99] = remote(99, invite["child"]["email"])
        return {**invite, "success": True, "pushed": True}

    # -- helpers

    async def start(self, new="new@icloud.com"):
        op, blocked = await mr.open_rotation(self.db, self.ws.id, email="old@icloud.com", replacement_email=new)
        self.assertIsNone(blocked)
        return op

    async def drive(self, op, **kw):
        result = await mr.run_manual_rotation(self.db, op.public_id, rotate=self.rotate, in_test=True, **kw)
        await operation_store.finish(self.db, op, result)
        await self.db.commit()
        return result

    async def steps(self, op):
        rows = await self.db.scalars(select(OperationStep).where(OperationStep.operation_id == op.id))
        return {row.step_name: row.state for row in rows}

    # -- tests

    def test_email_rules(self):
        self.assertIsNone(manual_rotation_email_error("a@x.com", "x@icloud.com"))
        for bad in ("", "x@icloud.com----https://pick.up", "not-an-email", "a b@x.com", "A@X.com"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(manual_rotation_email_error("a@x.com", bad))

    async def test_happy_path_order_inherits_seat_and_deletes_old_last(self):
        op = await self.start()
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        self.assertEqual(op.state, "success")
        call = self.onboard_calls[0]
        self.assertEqual((call["role"], call["seat_intent"]), ("member", "premium"))
        self.assertEqual(call["signup_flow"], "extension")
        self.assertFalse(call["use_phone_pool"])
        self.assertTrue(call["oauth_signup"] and call["keep_operation_identity"])
        self.assertEqual(call["email_line"], "new@icloud.com")
        order = [c[0] for c in self.sub.calls if c[0] in {"pause", "delete"}]
        self.assertEqual(order, ["pause", "delete"])
        self.assertNotIn(88, self.sub.remotes)
        self.assertIsNone(await self.db.get(ExternalBinding, self.old_binding.id))
        self.assertIsNotNone(await self.db.get(Account, self.old.id))  # Old profile kept.
        steps = await self.steps(op)
        for name in ("preflight", "paused", "kicked", "vacancy", "joined", "authorized", "published", "old_remote_deleted", "counted"):
            self.assertEqual(steps.get(name), "success", name)
        await self.db.refresh(self.ws)
        self.assertEqual(self.ws.manual_switch_count, 1)
        # Parent identity stays the old account; the new one is only in the result.
        self.assertEqual(op.email, "old@icloud.com")
        self.assertEqual(op.account_id, self.old.id)

    async def test_preflight_failure_never_pauses_or_kicks(self):
        self.members["old@icloud.com"]["seat_type"] = "mystery"
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "seat_unknown")
        self.assertEqual(op.state, "failed")
        self.assertEqual(self.sub.calls, [])
        self.assertIn("old@icloud.com", self.members)

    async def test_admin_role_is_not_downgraded(self):
        self.members["old@icloud.com"]["role"] = "admin"
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "role_unsupported")
        self.assertEqual(self.sub.calls, [])

    async def test_open_rejects_mother_same_email_and_second_rotation(self):
        for new, code in (("owner@example.com", "primary_mother_protected"), ("old@icloud.com", "invalid_rotation_email"),
                          ("x@icloud.com----https://p", "invalid_rotation_email")):
            with self.subTest(new=new):
                _, blocked = await mr.open_rotation(self.db, self.ws.id, email="old@icloud.com", replacement_email=new)
                self.assertEqual(blocked["error_code"], code)
        await self.start()
        _, blocked = await mr.open_rotation(self.db, self.ws.id, email="old@icloud.com", replacement_email="b@icloud.com")
        self.assertEqual(blocked["error_code"], "operation_conflict")

    async def test_phone_verification_keeps_old_remote_and_same_mailbox(self):
        self.onboard_outcome = "phone"
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(op.state, "manual_required")
        self.assertEqual(result["error_code"], "phone_verification_required")
        self.assertIn(88, self.sub.remotes)  # Paused, not deleted.
        self.assertFalse(self.sub.remotes[88]["schedulable"])
        self.assertTrue(self.publish_calls == [])
        # The team is blocked until this one is continued or archived.
        _, blocked = await mr.open_rotation(self.db, self.ws.id, email="old@icloud.com", replacement_email="c@icloud.com")
        self.assertEqual(blocked["error_code"], "rotation_unresolved")
        # Continue: same mailbox, no second kick, completes.
        self.onboard_outcome = "authorized"
        reopened, blocked = await mr.reopen_rotation(self.db, op.public_id)
        self.assertIsNone(blocked)
        result = await self.drive(reopened)
        self.assertTrue(result["success"], result)
        self.assertEqual([c["email_line"] for c in self.onboard_calls], ["new@icloud.com", "new@icloud.com"])
        self.assertEqual(sum(c[0] == "pause" for c in self.sub.calls), 1)

    async def test_publish_pending_never_deletes_old_and_resumes_publish_only(self):
        self.publish_ok = False
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(op.state, "partial")
        self.assertEqual(result["error_code"], "publish_pending")
        self.assertIn(88, self.sub.remotes)
        self.publish_ok = True
        await mr.reopen_rotation(self.db, op.public_id)
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.onboard_calls), 1)  # No second registration.
        self.assertTrue(self.publish_calls[-1]["publish_written"])  # Resume reads back, does not rewrite blindly.

    async def test_unhealthy_readback_blocks_old_delete(self):
        async def publish(db, invite, workspace_id):
            await self._publish(db, invite, workspace_id)
            self.sub.remotes[99]["schedulable"] = False
            return {**invite, "success": True}

        with patch.object(mr, "publish_replacement", new=publish):
            op = await self.start()
            result = await self.drive(op)
        self.assertEqual(result["error_code"], "new_not_schedulable")
        self.assertIn(88, self.sub.remotes)

    async def test_wrong_workspace_identity_blocks_old_delete(self):
        async def publish(db, invite, workspace_id):
            await self._publish(db, invite, workspace_id)
            self.sub.remotes[99]["credentials"]["workspace_id"] = "other"
            return {**invite, "success": True}

        with patch.object(mr, "publish_replacement", new=publish):
            op = await self.start()
            result = await self.drive(op)
        self.assertEqual(result["error_code"], "new_identity_unconfirmed")
        self.assertIn(88, self.sub.remotes)

    async def test_old_delete_timeout_then_continue_only_cleans_up_and_counts_once(self):
        self.sub.delete_ok = False
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_remote_delete_pending")
        self.assertTrue(result["new_available"])
        self.assertEqual(op.state, "partial")
        await self.db.refresh(self.ws)
        self.assertFalse(self.ws.manual_switch_count)
        self.sub.delete_ok = True
        await mr.reopen_rotation(self.db, op.public_id)
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.onboard_calls), 1)
        self.assertEqual(len(self.publish_calls), 1)
        await self.db.refresh(self.ws)
        self.assertEqual(self.ws.manual_switch_count, 1)
        # A finished rotation cannot be continued or counted again.
        _, blocked = await mr.reopen_rotation(self.db, op.public_id)
        self.assertEqual(blocked["error_code"], "continue_unsupported")

    async def test_unsafe_vacancy_stops_until_operator_confirms(self):
        self.kick_vacancy = {"is_free": False}
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "vacancy_not_safe_to_refill")
        self.assertEqual(self.onboard_calls, [])
        await mr.reopen_rotation(self.db, op.public_id)
        result = await self.drive(op, confirm_vacancy=True)
        self.assertTrue(result["success"], result)

    async def test_old_without_remote_skips_pause_and_delete(self):
        self.sub.remotes.pop(88)
        await self.db.delete(self.old_binding)
        await self.db.commit()
        op = await self.start()
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        self.assertEqual([c for c in self.sub.calls if c[0] in {"pause", "delete"}], [])

    async def test_cancel_after_kick_is_partial_not_rollback(self):
        op = await self.start()
        original = self.rotate.kick_to_standby

        async def kick_then_cancel(db, **kw):
            out = await original(db, **kw)
            row = await operation_store.get_by_public_id(db, kw["job_id"])
            row.cancel_requested = True
            await db.commit()
            return out

        self.rotate.kick_to_standby = kick_then_cancel
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "cancel_after_side_effect")
        self.assertEqual(op.state, "partial")
        self.assertEqual(self.onboard_calls, [])


    async def test_concurrent_cross_team_claims_and_continuations_are_serialized(self):
        import asyncio
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            engine = create_async_engine("sqlite+aiosqlite:///" + (Path(directory) / "claims.db").as_posix())
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as db:
                    db.add_all([Workspace(id=1, official_workspace_id="a", status="active"),
                                Workspace(id=2, official_workspace_id="b", status="active")])
                    await db.commit()
                async def claim(workspace_id):
                    async with sessions() as db:
                        op, error = await mr.open_rotation(db, workspace_id, email=f"old{workspace_id}@x.com", replacement_email="shared@x.com")
                        return op.public_id if op else None, error
                claims = await asyncio.gather(claim(1), claim(2))
                self.assertEqual(sum(bool(op) for op, _ in claims), 1, claims)
                public_id = next(op for op, _ in claims if op)
                async with sessions() as db:
                    op = await operation_store.get_by_public_id(db, public_id)
                    op.state = "partial"
                    await operation_store.mark_step(db, op, "paused", state="success")
                    await db.commit()
                async def resume():
                    async with sessions() as db:
                        op, error = await mr.reopen_rotation(db, public_id)
                        return op is not None, error
                resumes = await asyncio.gather(resume(), resume())
                self.assertEqual(sum(ok for ok, _ in resumes), 1, resumes)
            finally:
                await engine.dispose()

    async def test_all_inherited_role_seat_combinations(self):
        # Preflight reads official values, including on a full team.
        for role in ("account-owner", "standard-user"):
            for seat in ("default", "prolite"):
                with self.subTest(role=role, seat=seat):
                    self.members["old@icloud.com"].update(role=role, seat_type=seat)
                    ctx = await mr.preflight(self.db, self.rotate, self.ws.id, self.old.email, "new@icloud.com")
                    self.assertEqual(ctx["role"], "owner" if role == "account-owner" else "member")
                    self.assertEqual(ctx["seat_intent"], "standard" if seat == "default" else "premium")

    async def test_archiving_unfinished_rotation_does_not_unlock_team(self):
        self.onboard_outcome = "phone"
        op = await self.start()
        await self.drive(op)
        op.archived_at = mr.utcnow()
        await self.db.commit()
        _, blocked = await mr.open_rotation(self.db, self.ws.id, email=self.old.email, replacement_email="next@icloud.com")
        self.assertEqual(blocked["error_code"], "rotation_unresolved")
        reopened, blocked = await mr.reopen_rotation(self.db, op.public_id)
        self.assertIsNone(blocked)
        self.assertIsNone(reopened.archived_at)

    async def test_local_absence_does_not_hide_unbound_remote_account(self):
        await self.db.delete(self.old_binding)
        await self.db.commit()
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_binding_unverified")
        self.assertIn("old@icloud.com", self.members)
        self.assertTrue(self.sub.remotes[88]["schedulable"])

    async def _mark_old_binding_missing(self):
        self.old_binding.binding_state = "missing"
        await self.db.commit()

    async def test_binding_marked_missing_proceeds_when_catalog_confirms_absence(self):
        await self._mark_old_binding_missing()
        self.sub.remotes.pop(88)
        op = await self.start()
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        self.assertEqual([c for c in self.sub.calls if c[0] in {"pause", "delete"}], [])

    async def test_binding_marked_missing_but_remote_id_still_listed_blocks(self):
        await self._mark_old_binding_missing()
        self.sub.remotes[88] = remote(88, "someone-else@icloud.com")
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_binding_unverified")
        self.assertIn("old@icloud.com", self.members)
        self.assertTrue(self.sub.remotes[88]["schedulable"])

    async def test_binding_marked_missing_but_email_still_listed_blocks(self):
        await self._mark_old_binding_missing()
        self.sub.remotes.pop(88)
        self.sub.remotes[77] = remote(77, "old@icloud.com")
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_binding_unverified")
        self.assertIn("old@icloud.com", self.members)

    async def test_binding_marked_missing_with_unreadable_catalog_blocks(self):
        await self._mark_old_binding_missing()
        self.sub.remotes.pop(88)
        self.sub.list_status_accounts = AsyncMock(side_effect=RuntimeError("unauthorized"))
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_remote_unknown")
        self.assertIn("old@icloud.com", self.members)

    async def test_binding_marked_missing_with_malformed_catalog_blocks(self):
        await self._mark_old_binding_missing()
        self.sub.remotes.pop(88)
        self.sub.list_status_accounts = AsyncMock(return_value=[{"platform": "openai"}])
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_remote_unknown")
        self.assertIn("old@icloud.com", self.members)

    async def test_pending_binding_still_blocks(self):
        self.old_binding.binding_state = "pending"
        await self.db.commit()
        self.sub.remotes.pop(88)
        op = await self.start()
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "old_binding_unverified")

    async def test_new_official_membership_changed_prevents_old_deletion(self):
        async def publish(db, invite, workspace_id):
            result = await self._publish(db, invite, workspace_id)
            self.members["new@icloud.com"]["seat_type"] = "default"
            return result
        with patch.object(mr, "publish_replacement", new=publish):
            op = await self.start()
            result = await self.drive(op)
        self.assertEqual(result["error_code"], "new_membership_changed")
        self.assertIn(88, self.sub.remotes)

    async def test_manual_authorization_defers_push_and_count_to_rotation(self):
        from app.application.member_handoff import count_switch_once, finish_after_authorization
        self.onboard_outcome = "oauth_failed"
        op = await self.start()
        await self.drive(op)
        account = await self.db.scalar(select(Account).where(Account.email == "new@icloud.com"))
        followups = await finish_after_authorization(self.db, account.id, workspace_id=self.ws.id,
                                                     push_sub2api=True, count_switch=True)
        self.assertEqual(followups["sub2api"]["rotation_operation_id"], op.public_id)
        self.assertFalse((await count_switch_once(self.db, self.ws.id, account.id))["counted"])
        self.onboard_outcome = "authorized"
        await mr.reopen_rotation(self.db, op.public_id)
        result = await self.drive(op)
        self.assertTrue(result["success"], result)
        await self.db.refresh(self.ws)
        self.assertEqual(self.ws.manual_switch_count, 1)

    async def test_detail_404_is_not_deletion_evidence(self):
        op = await self.start()
        flow = mr._Rotation(self.db, op, self.rotate, confirm_vacancy=False, in_test=True)
        self.sub.get_account = AsyncMock(side_effect=httpx.HTTPStatusError("route missing",
            request=httpx.Request("GET", "http://sub.test"), response=httpx.Response(404)))
        self.assertIs(await flow._old_remote_absent(88), False)
        self.sub.list_status_accounts = AsyncMock(side_effect=RuntimeError("unauthorized"))
        self.assertIsNone(await flow._old_remote_absent(88))

    async def test_same_mailbox_cannot_be_used_by_another_team_or_onboard(self):
        op = await self.start()
        ws2 = Workspace(official_workspace_id="other", owner_account_id=self.owner.id, status="active")
        self.db.add(ws2)
        await self.db.commit()
        _, blocked = await mr.open_rotation(self.db, ws2.id, email="other@icloud.com", replacement_email="new@icloud.com")
        self.assertEqual(blocked["error_code"], "replacement_in_use")
        _, blocker = await operation_store.create_workspace_locked(self.db, op_type="onboard", workspace_id=ws2.id,
                                                                    email="new@icloud.com")
        self.assertEqual(blocker.public_id, op.public_id)

    async def test_changed_credentials_on_cleanup_resume_never_delete_old(self):
        self.sub.delete_ok = False
        op = await self.start()
        await self.drive(op)
        account = await self.db.scalar(select(Account).where(Account.email == "new@icloud.com"))
        account.credential_revision += 1
        await self.db.commit()
        self.sub.delete_ok = True
        await mr.reopen_rotation(self.db, op.public_id)
        result = await self.drive(op)
        self.assertEqual(result["error_code"], "credential_revision_changed")
        self.assertIn(88, self.sub.remotes)
        self.assertEqual(len(self.onboard_calls), 1)
        self.assertEqual(len(self.publish_calls), 1)

    async def test_lost_publish_result_reconciles_same_write_identity(self):
        self.publish_ok = False
        op = await self.start()
        await self.drive(op)
        await operation_store.mark_step(self.db, op, "published", state="running", result={
            "intent": "publish", "sub2api_operation_id": "write-before-crash"})
        await self.db.commit()
        await mr.reopen_rotation(self.db, op.public_id)
        await self.drive(op)
        self.assertTrue(self.publish_calls[-1]["publish_written"])
        self.assertEqual(self.publish_calls[-1]["sub2api_operation_id"], "write-before-crash")
        self.assertEqual(len(self.onboard_calls), 1)
        self.assertIn(88, self.sub.remotes)


class RotateApiTests(unittest.TestCase):
    def test_request_requires_single_replacement_mailbox(self):
        import tempfile
        from pathlib import Path

        from tests.helpers import make_client

        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            for body in ({"email": "old@x.com"}, {"email": "old@x.com", "replacement_email": "old@x.com"},
                         {"email": "old@x.com", "replacement_email": "a@x.com----https://p"}):
                with self.subTest(body=body):
                    self.assertEqual(client.post("/api/workspaces/1/rotate", json=body).status_code, 422)
            self.assertEqual(client.post("/api/operations/missing/continue-rotation").status_code, 404)


if __name__ == "__main__":
    unittest.main()
