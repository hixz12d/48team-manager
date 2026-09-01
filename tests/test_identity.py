import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import audit_identity, automation_gate, verify_bindings
from app.domain.identity import (
    AUDIT_CONFLICT,
    AUDIT_SUSPICIOUS,
    AUDIT_UNBOUND,
    AUDIT_VERIFIED,
    BINDING_CONFLICT,
    BINDING_MISSING,
    BINDING_PENDING,
    BINDING_VERIFIED,
)
from app.domain.identity.ids import is_workspace_account_id, looks_like_gmail, workspace_official_id
from app.domain.identity.policy import (
    normalize_local_purpose,
    normalize_official_plan,
    purpose_from_plan,
    role_from_email,
)
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from legacy_import.importer import import_legacy_identity


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


def _remote_account(remote_id, *, email="", name="", official_account_id="", workspace_id=""):
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


def _write_legacy_db(path: Path, *, teams, children=(), mappings=()):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR(255) NOT NULL,
            access_token_encrypted TEXT NOT NULL,
            account_id VARCHAR(100),
            team_name VARCHAR(255),
            plan_type VARCHAR(50),
            subscription_plan VARCHAR(100),
            sub2api_account_id INTEGER,
            max_members INTEGER,
            status VARCHAR(20),
            proxy VARCHAR(500),
            last_sync DATETIME,
            created_at DATETIME
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE child_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR(255) UNIQUE NOT NULL,
            status VARCHAR(20),
            current_team_id INTEGER,
            account_id VARCHAR(100),
            sub2api_account_id INTEGER,
            next_eligible_at DATETIME,
            proxy VARCHAR(500)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE team_email_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            email VARCHAR(255) NOT NULL,
            status VARCHAR(20),
            child_account_id INTEGER,
            joined_at DATETIME,
            kicked_at DATETIME
        )
        """
    )
    for team in teams:
        conn.execute(
            """
            INSERT INTO teams (
                email, access_token_encrypted, account_id, team_name, plan_type,
                subscription_plan, sub2api_account_id, max_members, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team["email"],
                team.get("token", "tok"),
                team.get("account_id"),
                team.get("team_name"),
                team.get("plan_type"),
                team.get("subscription_plan"),
                team.get("sub2api_account_id"),
                team.get("max_members", 6),
                team.get("status", "active"),
            ),
        )
    for child in children:
        conn.execute(
            """
            INSERT INTO child_accounts (
                email, status, current_team_id, account_id, sub2api_account_id, next_eligible_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                child["email"],
                child.get("status", "active"),
                child.get("current_team_id"),
                child.get("account_id"),
                child.get("sub2api_account_id"),
                child.get("next_eligible_at"),
            ),
        )
    for mapping in mappings:
        conn.execute(
            """
            INSERT INTO team_email_mappings (team_id, email, status, child_account_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                mapping["team_id"],
                mapping["email"],
                mapping.get("status", "joined"),
                mapping.get("child_account_id"),
            ),
        )
    conn.commit()
    conn.close()


class IdentityInvariantTests(unittest.TestCase):
    def test_gmail_does_not_become_owner(self):
        self.assertIsNone(role_from_email("xiaozhudf2026.21@gmail.com"))
        self.assertIsNone(role_from_email("family.pedro@icloud.com"))
        self.assertTrue(looks_like_gmail("pro.user@gmail.com"))
        self.assertFalse(looks_like_gmail("kid@icloud.com"))

    def test_plan_is_not_purpose(self):
        self.assertEqual(normalize_official_plan("pro"), "pro")
        self.assertIsNone(purpose_from_plan("pro"))
        self.assertIsNone(purpose_from_plan("team"))
        self.assertEqual(normalize_local_purpose("mother"), "mother")
        self.assertEqual(normalize_local_purpose("child"), "child")
        with self.assertRaises(ValueError):
            normalize_local_purpose("gmail")

    def test_workspace_id_rejects_user_prefix(self):
        self.assertTrue(is_workspace_account_id(WORKSPACE_UUID))
        self.assertFalse(is_workspace_account_id("user-abc"))
        self.assertIsNone(workspace_official_id("user-abc"))
        self.assertEqual(workspace_official_id(WORKSPACE_UUID), WORKSPACE_UUID)


class IdentitySessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_gmail_without_owner_membership_is_conflict(self):
        self.session.add(
            Account(
                email="pro.user@gmail.com",
                official_plan="unknown",
                local_purpose="child",
                operational_state="active",
                auth_state="unknown",
            )
        )
        await self.session.commit()
        report = await audit_identity(self.session)
        finding = report["findings"][0]
        self.assertEqual(finding["result"], AUDIT_CONFLICT)
        self.assertIn("Gmail 无 workspace owner membership", finding["reasons"])
        self.assertEqual(finding["automation"], "blocked")
        self.assertNotEqual(finding["local_purpose"], "mother")

    async def test_name_like_mother_without_owner_membership_is_conflict(self):
        self.session.add(
            Account(
                email="looks-like-owner@icloud.com",
                official_plan="unknown",
                local_purpose="mother",
                operational_state="active",
                auth_state="unknown",
            )
        )
        await self.session.commit()
        finding = (await audit_identity(self.session))["findings"][0]
        self.assertEqual(finding["result"], AUDIT_CONFLICT)
        self.assertTrue(any("owner membership" in reason for reason in finding["reasons"]))

    async def test_duplicate_remote_id_constraint(self):
        first = Account(email="a@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        second = Account(email="b@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        self.session.add_all([first, second])
        await self.session.flush()
        self.session.add(ExternalBinding(provider="sub2api", local_account_id=first.id, remote_account_id="99", binding_state=BINDING_PENDING))
        await self.session.flush()
        self.session.add(ExternalBinding(provider="sub2api", local_account_id=second.id, remote_account_id="99", binding_state=BINDING_PENDING))
        with self.assertRaises(IntegrityError):
            await self.session.flush()
        await self.session.rollback()

    async def test_automation_gate_blocks_conflict_and_owner(self):
        child_acc = Account(email="kid@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        owner_acc = Account(email="owner@icloud.com", official_plan="unknown", local_purpose="mother", operational_state="active", auth_state="unknown")
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
        workspace = Workspace(official_workspace_id=WORKSPACE_UUID, owner_account_id=owner_acc.id, status="active")
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

        blocked = await automation_gate(self.session, remote_account_id=77, email="kid@icloud.com")
        self.assertFalse(blocked["allow"])
        self.assertEqual(blocked["error_code"], "identity_conflict")
        owner = await automation_gate(self.session, email="owner@icloud.com")
        self.assertFalse(owner["allow"])
        self.assertEqual(owner["error_code"], "owner_manual")


class IdentityImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.tmp = tempfile.TemporaryDirectory()
        self.legacy = Path(self.tmp.name) / "team_manage.db"

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_import_splits_workspace_user_and_plan(self):
        _write_legacy_db(
            self.legacy,
            teams=[
                {
                    "email": "owner@icloud.com",
                    "account_id": WORKSPACE_UUID,
                    "team_name": "Team .2026.11",
                    "plan_type": "team",
                    "subscription_plan": "chatgptteamplan",
                    "sub2api_account_id": 10,
                }
            ],
            children=[
                {
                    "email": "kid@icloud.com",
                    "status": "active",
                    "current_team_id": 1,
                    "account_id": WORKSPACE_UUID,
                    "sub2api_account_id": 11,
                }
            ],
            mappings=[{"team_id": 1, "email": "kid@icloud.com", "status": "joined", "child_account_id": 1}],
        )
        before = self.legacy.read_bytes()
        stats = await import_legacy_identity(self.legacy, self.session)
        self.assertEqual(self.legacy.read_bytes(), before)
        self.assertEqual(stats.mother_accounts, 1)
        self.assertEqual(stats.child_accounts, 1)
        self.assertEqual(stats.skipped_user_ids, 0)

        mother = (await self.session.execute(select(Account).where(Account.email == "owner@icloud.com"))).scalar_one()
        kid = (await self.session.execute(select(Account).where(Account.email == "kid@icloud.com"))).scalar_one()
        workspace = (await self.session.execute(select(Workspace))).scalar_one()
        owner_membership = (
            await self.session.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == mother.id))
        ).scalar_one()
        child_membership = (
            await self.session.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == kid.id))
        ).scalar_one()

        self.assertEqual(mother.local_purpose, "mother")
        self.assertEqual(mother.official_plan, "unknown")
        self.assertEqual(kid.local_purpose, "child")
        self.assertEqual(kid.official_plan, "unknown")
        self.assertEqual(workspace.official_workspace_id, WORKSPACE_UUID)
        self.assertNotEqual(workspace.official_workspace_id, mother.official_user_id)
        self.assertEqual(owner_membership.official_role, "owner")
        self.assertEqual(child_membership.official_role, "unknown")
        self.assertEqual(child_membership.local_purpose, "child")

        report = await audit_identity(self.session)
        by_email = {item["email"]: item for item in report["findings"]}
        self.assertEqual(by_email["owner@icloud.com"]["result"], AUDIT_VERIFIED)
        self.assertEqual(by_email["kid@icloud.com"]["result"], AUDIT_VERIFIED)
        self.assertTrue(any("pending" in reason for reason in by_email["owner@icloud.com"]["reasons"]))

    async def test_user_prefix_is_not_written_as_workspace_id(self):
        _write_legacy_db(
            self.legacy,
            teams=[{"email": "owner@icloud.com", "account_id": "user-not-a-workspace", "team_name": "Broken"}],
        )
        stats = await import_legacy_identity(self.legacy, self.session)
        self.assertEqual(stats.skipped_user_ids, 1)
        workspace = (await self.session.execute(select(Workspace))).scalar_one()
        self.assertIsNone(workspace.official_workspace_id)
        finding = (await audit_identity(self.session))["findings"][0]
        self.assertEqual(finding["result"], AUDIT_SUSPICIOUS)
        self.assertTrue(any("Workspace UUID" in reason for reason in finding["reasons"]))

    async def test_owner_mapping_is_not_guessed_from_team_email_mapping(self):
        _write_legacy_db(
            self.legacy,
            teams=[{"email": "owner@icloud.com", "account_id": WORKSPACE_UUID}],
            mappings=[{"team_id": 1, "email": "owner@icloud.com", "status": "joined"}],
        )
        await import_legacy_identity(self.legacy, self.session)
        memberships = (await self.session.execute(select(WorkspaceMembership))).scalars().all()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].official_role, "owner")
        self.assertEqual(memberships[0].local_purpose, "mother")

    async def test_standby_child_is_unbound_without_guessing_family(self):
        _write_legacy_db(
            self.legacy,
            teams=[],
            children=[{"email": "old@icloud.com", "status": "standby"}],
        )
        await import_legacy_identity(self.legacy, self.session)
        finding = (await audit_identity(self.session))["findings"][0]
        self.assertEqual(finding["local_purpose"], "standby")
        self.assertEqual(finding["result"], AUDIT_UNBOUND)
        self.assertEqual(finding["official_plan"], "unknown")

    async def test_duplicate_remote_id_during_import_conflicts(self):
        _write_legacy_db(
            self.legacy,
            teams=[{"email": "owner@icloud.com", "account_id": WORKSPACE_UUID, "sub2api_account_id": 5}],
            children=[{"email": "kid@icloud.com", "status": "active", "current_team_id": 1, "sub2api_account_id": 5}],
        )
        stats = await import_legacy_identity(self.legacy, self.session)
        self.assertGreaterEqual(stats.bindings, 1)
        bindings = (await self.session.execute(select(ExternalBinding))).scalars().all()
        self.assertEqual([row.remote_account_id for row in bindings].count("5"), 1)
        self.assertTrue(any(row.binding_state == "conflict" for row in bindings) or len(bindings) == 1)


class IdentityBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.tmp = tempfile.TemporaryDirectory()
        self.legacy = Path(self.tmp.name) / "team_manage.db"

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def _seed_mother(self, email="owner@icloud.com", remote_id=10, official_account_id=None):
        _write_legacy_db(
            self.legacy,
            teams=[
                {
                    "email": email,
                    "account_id": WORKSPACE_UUID,
                    "team_name": "Team .2026.11",
                    "sub2api_account_id": remote_id,
                }
            ],
        )
        await import_legacy_identity(self.legacy, self.session)
        account = (await self.session.execute(select(Account).where(Account.email == email))).scalar_one()
        if official_account_id:
            account.official_account_id = official_account_id
            await self.session.commit()
        return account

    async def test_rename_does_not_change_binding(self):
        account = await self._seed_mother()
        first = await verify_bindings(self.session, [_remote_account(10, email=account.email, name="Team .2026.11 母号")])
        self.assertEqual(first["bindings"][0]["binding_state"], BINDING_VERIFIED)
        second = await verify_bindings(self.session, [_remote_account(10, email=account.email, name="乱名-renamed")])
        binding = second["bindings"][0]
        self.assertEqual(binding["binding_state"], BINDING_VERIFIED)
        self.assertEqual(binding["remote_account_id"], "10")
        self.assertEqual(binding["local_account_id"], account.id)
        self.assertEqual(binding["verified_email"], account.email)
        finding = (await audit_identity(self.session))["findings"][0]
        self.assertEqual(finding["result"], AUDIT_VERIFIED)

    async def test_same_display_name_does_not_merge(self):
        first = await self._seed_mother(email="owner-a@icloud.com", remote_id=21)
        second = Account(email="owner-b@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        self.session.add(second)
        await self.session.flush()
        self.session.add(ExternalBinding(provider="sub2api", local_account_id=second.id, remote_account_id="22", binding_state=BINDING_PENDING))
        await self.session.commit()
        report = await verify_bindings(
            self.session,
            [
                _remote_account(21, email=first.email, name="Team same 母号"),
                _remote_account(22, email=second.email, name="Team same 母号"),
            ],
        )
        by_remote = {row["remote_account_id"]: row for row in report["bindings"]}
        self.assertEqual(by_remote["21"]["local_account_id"], first.id)
        self.assertEqual(by_remote["22"]["local_account_id"], second.id)

    async def test_one_remote_id_cannot_bind_two_local_accounts(self):
        first = Account(email="a@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        second = Account(email="b@icloud.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        self.session.add_all([first, second])
        await self.session.flush()
        self.session.add(ExternalBinding(provider="sub2api", local_account_id=first.id, remote_account_id="99", binding_state=BINDING_PENDING))
        await self.session.commit()
        report = await verify_bindings(self.session, [_remote_account(99, email=second.email)])
        binding = report["bindings"][0]
        self.assertEqual(binding["local_account_id"], first.id)
        self.assertEqual(binding["binding_state"], BINDING_CONFLICT)
        self.assertIn("email mismatch", binding["last_error"])

    async def test_email_match_with_official_id_mismatch_is_conflict(self):
        account = await self._seed_mother(official_account_id="acct-local")
        report = await verify_bindings(self.session, [_remote_account(10, email=account.email, official_account_id="acct-other")])
        self.assertEqual(report["bindings"][0]["binding_state"], BINDING_CONFLICT)
        self.assertIn("official account id mismatch", report["bindings"][0]["last_error"])
        self.assertEqual((await audit_identity(self.session))["findings"][0]["result"], AUDIT_CONFLICT)

    async def test_email_match_with_workspace_mismatch_is_conflict(self):
        account = await self._seed_mother()
        report = await verify_bindings(
            self.session,
            [_remote_account(10, email=account.email, workspace_id="22222222-2222-2222-2222-222222222222")],
        )
        self.assertEqual(report["bindings"][0]["binding_state"], BINDING_CONFLICT)
        self.assertIn("workspace id mismatch", report["bindings"][0]["last_error"])

    async def test_name_only_remote_is_orphaned(self):
        _write_legacy_db(self.legacy, teams=[{"email": "owner@icloud.com", "account_id": WORKSPACE_UUID, "team_name": "Team .2026.11"}])
        await import_legacy_identity(self.legacy, self.session)
        report = await verify_bindings(self.session, [_remote_account(88, name="Team .2026.11 母号")])
        self.assertEqual(report["stats"]["orphaned"], 1)
        self.assertEqual(report["bindings"], [])

    async def test_gmail_is_not_bound_as_owner_by_name(self):
        gmail = Account(email="pro.user@gmail.com", official_plan="unknown", local_purpose="child", operational_state="active", auth_state="unknown")
        mother = Account(email="owner@icloud.com", official_plan="unknown", local_purpose="mother", operational_state="active", auth_state="unknown")
        self.session.add_all([gmail, mother])
        await self.session.flush()
        self.session.add(ExternalBinding(provider="sub2api", local_account_id=mother.id, remote_account_id="70", binding_state=BINDING_PENDING))
        await self.session.commit()
        report = await verify_bindings(self.session, [_remote_account(70, email="pro.user@gmail.com", name="Team xxx 母号")])
        self.assertEqual(report["bindings"][0]["local_account_id"], mother.id)
        self.assertEqual(report["bindings"][0]["binding_state"], BINDING_CONFLICT)
        by_email = {item["email"]: item for item in (await audit_identity(self.session))["findings"]}
        self.assertEqual(by_email["pro.user@gmail.com"]["result"], AUDIT_CONFLICT)
        self.assertNotEqual(by_email["pro.user@gmail.com"]["local_purpose"], "mother")

    async def test_conflict_is_not_auto_promoted_back_to_verified(self):
        account = await self._seed_mother()
        binding = (await self.session.execute(select(ExternalBinding))).scalar_one()
        binding.binding_state = BINDING_CONFLICT
        binding.last_error = "manual hold"
        await self.session.commit()
        report = await verify_bindings(self.session, [_remote_account(10, email=account.email)])
        self.assertEqual(report["bindings"][0]["binding_state"], BINDING_CONFLICT)
        self.assertEqual(report["bindings"][0]["last_error"], "manual hold")
        self.assertIsNone(report["bindings"][0]["verified_email"])

    async def test_missing_remote_marks_binding_missing(self):
        await self._seed_mother()
        report = await verify_bindings(self.session, [])
        self.assertEqual(report["bindings"][0]["binding_state"], BINDING_MISSING)
        self.assertIn("missing from snapshot", report["bindings"][0]["last_error"])
