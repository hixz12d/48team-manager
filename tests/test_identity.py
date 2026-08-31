import os
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.models import Account, ChildAccount, ExternalBinding, Team, TeamEmailMapping, Workspace, WorkspaceMembership
from app.services.child_accounts import is_workspace_account_id
from app.services.identity import (
    AUDIT_CONFLICT,
    AUDIT_SUSPICIOUS,
    AUDIT_UNBOUND,
    AUDIT_VERIFIED,
    BINDING_CONFLICT,
    BINDING_MISSING,
    BINDING_PENDING,
    BINDING_VERIFIED,
    identity_service,
    looks_like_gmail,
    workspace_official_id,
)


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


class IdentityHelperTests(unittest.TestCase):
    def test_gmail_is_not_automatically_mother(self):
        self.assertTrue(looks_like_gmail("pro.user@gmail.com"))
        self.assertFalse(looks_like_gmail("kid@icloud.com"))

    def test_workspace_id_rejects_user_prefix(self):
        self.assertTrue(is_workspace_account_id(WORKSPACE_UUID))
        self.assertFalse(is_workspace_account_id("user-abc"))
        team = Team(email="owner@example.com", access_token_encrypted="x", account_id="user-abc")
        self.assertIsNone(workspace_official_id(team))
        team.account_id = WORKSPACE_UUID
        self.assertEqual(workspace_official_id(team), WORKSPACE_UUID)


class IdentityMigrationTests(unittest.TestCase):
    def test_run_auto_migration_creates_identity_tables_without_dropping_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy.db")
            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL,
                    access_token_encrypted TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            run_auto_migration(db_path)

            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            }
            conn.close()

            self.assertIn("teams", tables)
            self.assertIn("accounts", tables)
            self.assertIn("workspaces", tables)
            self.assertIn("workspace_memberships", tables)
            self.assertIn("external_bindings", tables)
            self.assertIn("quota_snapshots", tables)
            self.assertIn("operations", tables)
            self.assertIn("operation_steps", tables)
            self.assertIn("uq_external_binding_remote", indexes)


class IdentityBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_backfill_splits_workspace_user_and_plan(self):
        team = Team(
            email="owner@icloud.com",
            access_token_encrypted="tok",
            account_id=WORKSPACE_UUID,
            team_name="Team .2026.11",
            plan_type="team",
            subscription_plan="chatgptteamplan",
            sub2api_account_id=10,
            max_members=6,
            status="active",
        )
        self.session.add(team)
        await self.session.flush()
        child = ChildAccount(
            email="kid@icloud.com",
            status="active",
            current_team_id=team.id,
            account_id=WORKSPACE_UUID,
            sub2api_account_id=11,
            next_eligible_at=None,
        )
        self.session.add(child)
        await self.session.flush()
        self.session.add(
            TeamEmailMapping(
                team_id=team.id,
                email="kid@icloud.com",
                status="joined",
                child_account_id=child.id,
            )
        )
        await self.session.commit()

        stats = await identity_service.backfill(self.session)
        self.assertEqual(stats["mother_accounts"], 1)
        self.assertEqual(stats["child_accounts"], 1)
        self.assertEqual(stats["skipped_user_ids"], 0)

        mother = (await self.session.execute(select(Account).where(Account.email == "owner@icloud.com"))).scalar_one()
        kid = (await self.session.execute(select(Account).where(Account.email == "kid@icloud.com"))).scalar_one()
        workspace = (await self.session.execute(select(Workspace))).scalar_one()
        owner_membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == mother.id)
            )
        ).scalar_one()
        child_membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == kid.id)
            )
        ).scalar_one()

        self.assertEqual(mother.local_purpose, "mother")
        self.assertEqual(mother.official_plan, "unknown")
        self.assertEqual(kid.local_purpose, "child")
        self.assertEqual(kid.official_plan, "unknown")
        self.assertEqual(workspace.official_workspace_id, WORKSPACE_UUID)
        self.assertNotEqual(workspace.official_workspace_id, mother.official_user_id)
        self.assertEqual(owner_membership.official_role, "owner")
        self.assertEqual(owner_membership.local_purpose, "mother")
        self.assertEqual(child_membership.official_role, "unknown")
        self.assertEqual(child_membership.local_purpose, "child")

        report = await identity_service.audit(self.session)
        by_email = {item["email"]: item for item in report["findings"]}
        self.assertEqual(by_email["owner@icloud.com"]["result"], AUDIT_VERIFIED)
        self.assertEqual(by_email["kid@icloud.com"]["result"], AUDIT_VERIFIED)
        self.assertTrue(
            any("pending" in reason for reason in by_email["owner@icloud.com"]["reasons"])
        )
        self.assertTrue(all(item["official_plan"] == "unknown" for item in report["findings"]))

        leftover_team = await self.session.get(Team, team.id)
        leftover_child = await self.session.get(ChildAccount, child.id)
        self.assertEqual(leftover_team.email, "owner@icloud.com")
        self.assertEqual(leftover_child.status, "active")

    async def test_user_prefix_is_not_written_as_workspace_id(self):
        team = Team(
            email="owner@icloud.com",
            access_token_encrypted="tok",
            account_id="user-not-a-workspace",
            team_name="Broken",
        )
        self.session.add(team)
        await self.session.commit()

        stats = await identity_service.backfill(self.session)
        self.assertEqual(stats["skipped_user_ids"], 1)
        workspace = (await self.session.execute(select(Workspace))).scalar_one()
        self.assertIsNone(workspace.official_workspace_id)

        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_SUSPICIOUS)
        self.assertTrue(any("Workspace UUID" in reason for reason in finding["reasons"]))

    async def test_gmail_without_owner_membership_is_conflict(self):
        gmail = Account(
            email="pro.user@gmail.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add(gmail)
        await self.session.commit()

        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_CONFLICT)
        self.assertIn("Gmail 无 workspace owner membership", finding["reasons"])
        self.assertEqual(finding["automation"], "blocked")
        self.assertEqual(finding["official_plan"], "unknown")
        self.assertNotEqual(finding["local_purpose"], "mother")

    async def test_name_like_mother_without_owner_membership_is_conflict(self):
        account = Account(
            email="looks-like-owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add(account)
        await self.session.commit()

        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_CONFLICT)
        self.assertTrue(any("owner membership" in reason for reason in finding["reasons"]))

    async def test_duplicate_remote_id_constraint_and_backfill_conflict(self):
        first = Account(email="a@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        second = Account(email="b@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        self.session.add_all([first, second])
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=first.id,
                remote_account_id="99",
                binding_state=BINDING_PENDING,
            )
        )
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=second.id,
                remote_account_id="99",
                binding_state=BINDING_PENDING,
            )
        )
        with self.assertRaises(IntegrityError):
            await self.session.flush()
        await self.session.rollback()

        team = Team(email="owner@icloud.com", access_token_encrypted="tok", account_id=WORKSPACE_UUID, sub2api_account_id=5)
        self.session.add(team)
        await self.session.flush()
        self.session.add(ChildAccount(email="kid@icloud.com", status="active", current_team_id=team.id, sub2api_account_id=5))
        await self.session.commit()

        stats = await identity_service.backfill(self.session)
        self.assertGreaterEqual(stats["bindings"], 1)
        bindings = (await self.session.execute(select(ExternalBinding))).scalars().all()
        remote_ids = [row.remote_account_id for row in bindings]
        self.assertEqual(remote_ids.count("5"), 1)
        self.assertTrue(any(row.binding_state == "conflict" for row in bindings) or len(bindings) == 1)

        report = await identity_service.audit(self.session)
        kid = next(item for item in report["findings"] if item["email"] == "kid@icloud.com")
        self.assertIn(kid["result"], {AUDIT_CONFLICT, AUDIT_UNBOUND, AUDIT_SUSPICIOUS})

    async def test_owner_mapping_is_not_guessed_from_team_email_mapping(self):
        team = Team(
            email="owner@icloud.com",
            access_token_encrypted="tok",
            account_id=WORKSPACE_UUID,
        )
        self.session.add(team)
        await self.session.flush()
        self.session.add(
            TeamEmailMapping(team_id=team.id, email="owner@icloud.com", status="joined")
        )
        await self.session.commit()

        await identity_service.backfill(self.session)
        memberships = (await self.session.execute(select(WorkspaceMembership))).scalars().all()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].official_role, "owner")
        self.assertEqual(memberships[0].local_purpose, "mother")

    async def test_standby_child_is_unbound_without_guessing_family(self):
        child = ChildAccount(email="old@icloud.com", status="standby")
        self.session.add(child)
        await self.session.commit()

        await identity_service.backfill(self.session)
        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["local_purpose"], "standby")
        self.assertEqual(finding["result"], AUDIT_UNBOUND)
        self.assertEqual(finding["official_plan"], "unknown")


def _remote_account(
    remote_id,
    *,
    email="",
    name="",
    official_account_id="",
    workspace_id="",
):
    credentials = {}
    extra = {}
    if email:
        credentials["email"] = email
        extra["email"] = email
    if official_account_id:
        credentials["chatgpt_account_id"] = official_account_id
    if workspace_id:
        extra["workspace_id"] = workspace_id
    return {
        "id": remote_id,
        "name": name,
        "credentials": credentials,
        "extra": extra,
    }


class IdentityBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed_mother(self, email="owner@icloud.com", remote_id=10, official_account_id=None):
        team = Team(
            email=email,
            access_token_encrypted="tok",
            account_id=WORKSPACE_UUID,
            team_name="Team .2026.11",
            sub2api_account_id=remote_id,
            status="active",
        )
        self.session.add(team)
        await self.session.commit()
        await identity_service.backfill(self.session)
        account = (await self.session.execute(select(Account).where(Account.email == email))).scalar_one()
        if official_account_id:
            account.official_account_id = official_account_id
            await self.session.commit()
        return account

    async def test_rename_does_not_change_binding(self):
        account = await self._seed_mother()
        first = await identity_service.verify_bindings(
            self.session,
            [_remote_account(10, email=account.email, name="Team .2026.11 母号")],
        )
        self.assertEqual(first["bindings"][0]["binding_state"], BINDING_VERIFIED)

        second = await identity_service.verify_bindings(
            self.session,
            [_remote_account(10, email=account.email, name="乱名-renamed")],
        )
        binding = second["bindings"][0]
        self.assertEqual(binding["binding_state"], BINDING_VERIFIED)
        self.assertEqual(binding["remote_account_id"], "10")
        self.assertEqual(binding["local_account_id"], account.id)
        self.assertEqual(binding["verified_email"], account.email)

        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_VERIFIED)
        self.assertTrue(any("已交叉验证" in reason for reason in finding["reasons"]))

    async def test_same_display_name_does_not_merge(self):
        first = await self._seed_mother(email="owner-a@icloud.com", remote_id=21)
        second = Account(
            email="owner-b@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add(second)
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=second.id,
                remote_account_id="22",
                binding_state=BINDING_PENDING,
            )
        )
        await self.session.commit()

        report = await identity_service.verify_bindings(
            self.session,
            [
                _remote_account(21, email=first.email, name="Team same 母号"),
                _remote_account(22, email=second.email, name="Team same 母号"),
            ],
        )
        by_remote = {row["remote_account_id"]: row for row in report["bindings"]}
        self.assertEqual(by_remote["21"]["local_account_id"], first.id)
        self.assertEqual(by_remote["22"]["local_account_id"], second.id)
        self.assertNotEqual(by_remote["21"]["local_account_id"], by_remote["22"]["local_account_id"])

    async def test_one_remote_id_cannot_bind_two_local_accounts(self):
        first = Account(
            email="a@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        second = Account(
            email="b@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add_all([first, second])
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=first.id,
                remote_account_id="99",
                binding_state=BINDING_PENDING,
            )
        )
        await self.session.commit()

        report = await identity_service.verify_bindings(
            self.session,
            [_remote_account(99, email=second.email)],
        )
        binding = report["bindings"][0]
        self.assertEqual(binding["local_account_id"], first.id)
        self.assertEqual(binding["binding_state"], BINDING_CONFLICT)
        self.assertIn("email mismatch", binding["last_error"])

    async def test_email_match_with_official_id_mismatch_is_conflict(self):
        account = await self._seed_mother(official_account_id="acct-local")
        report = await identity_service.verify_bindings(
            self.session,
            [_remote_account(10, email=account.email, official_account_id="acct-other")],
        )
        binding = report["bindings"][0]
        self.assertEqual(binding["binding_state"], BINDING_CONFLICT)
        self.assertIn("official account id mismatch", binding["last_error"])

        audit = await identity_service.audit(self.session)
        finding = audit["findings"][0]
        self.assertEqual(finding["result"], AUDIT_CONFLICT)
        self.assertEqual(finding["automation"], "blocked")

    async def test_email_match_with_workspace_mismatch_is_conflict(self):
        account = await self._seed_mother()
        report = await identity_service.verify_bindings(
            self.session,
            [
                _remote_account(
                    10,
                    email=account.email,
                    workspace_id="22222222-2222-2222-2222-222222222222",
                )
            ],
        )
        binding = report["bindings"][0]
        self.assertEqual(binding["binding_state"], BINDING_CONFLICT)
        self.assertIn("workspace id mismatch", binding["last_error"])

    async def test_name_only_remote_is_orphaned(self):
        await self._seed_mother(remote_id=None)
        report = await identity_service.verify_bindings(
            self.session,
            [_remote_account(88, name="Team .2026.11 母号")],
        )
        self.assertEqual(report["stats"]["orphaned"], 1)
        self.assertEqual(report["bindings"], [])
        self.assertEqual(report["orphans"][0]["remote_account_id"], "88")

    async def test_gmail_is_not_bound_as_owner_by_name(self):
        gmail = Account(
            email="pro.user@gmail.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        mother = Account(
            email="owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add_all([gmail, mother])
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=mother.id,
                remote_account_id="70",
                binding_state=BINDING_PENDING,
            )
        )
        await self.session.commit()

        report = await identity_service.verify_bindings(
            self.session,
            [_remote_account(70, email="pro.user@gmail.com", name="Team xxx 母号")],
        )
        binding = report["bindings"][0]
        self.assertEqual(binding["local_account_id"], mother.id)
        self.assertEqual(binding["binding_state"], BINDING_CONFLICT)
        self.assertIn("email mismatch", binding["last_error"])

        audit = await identity_service.audit(self.session)
        by_email = {item["email"]: item for item in audit["findings"]}
        self.assertEqual(by_email["pro.user@gmail.com"]["result"], AUDIT_CONFLICT)
        self.assertNotEqual(by_email["pro.user@gmail.com"]["local_purpose"], "mother")

    async def test_conflict_is_not_auto_promoted_back_to_verified(self):
        account = await self._seed_mother()
        binding = (await self.session.execute(select(ExternalBinding))).scalar_one()
        binding.binding_state = BINDING_CONFLICT
        binding.last_error = "manual hold"
        await self.session.commit()

        report = await identity_service.verify_bindings(
            self.session,
            [_remote_account(10, email=account.email)],
        )
        row = report["bindings"][0]
        self.assertEqual(row["binding_state"], BINDING_CONFLICT)
        self.assertEqual(row["last_error"], "manual hold")
        self.assertIsNone(row["verified_email"])

    async def test_missing_remote_marks_binding_missing(self):
        await self._seed_mother()
        report = await identity_service.verify_bindings(self.session, [])
        row = report["bindings"][0]
        self.assertEqual(row["binding_state"], BINDING_MISSING)
        self.assertIn("missing from snapshot", row["last_error"])

    async def test_pending_without_cross_check_does_not_downgrade_identity_audit(self):
        await self._seed_mother()
        report = await identity_service.audit(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_VERIFIED)
        self.assertTrue(any("pending" in reason for reason in finding["reasons"]))

    async def test_automation_gate_blocks_conflict_and_owner(self):
        child_acc = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
        )
        owner_acc = Account(
            email="owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="unknown",
        )
        self.session.add_all([child_acc, owner_acc])
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=child_acc.id,
                remote_account_id="77",
                binding_state=BINDING_CONFLICT,
                last_error="email mismatch",
            )
        )
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner_acc.id,
            status="active",
        )
        self.session.add(workspace)
        await self.session.flush()
        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=owner_acc.id,
                official_role="owner",
                membership_state="joined",
                local_purpose="mother",
            )
        )
        await self.session.commit()

        blocked = await identity_service.automation_gate(
            self.session,
            remote_account_id=77,
            email="kid@icloud.com",
        )
        self.assertFalse(blocked["allow"])
        self.assertEqual(blocked["error_code"], "identity_conflict")

        owner = await identity_service.automation_gate(
            self.session,
            email="owner@icloud.com",
        )
        self.assertFalse(owner["allow"])
        self.assertEqual(owner["error_code"], "owner_manual")


if __name__ == "__main__":
    unittest.main()
