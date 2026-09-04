import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import ensure_membership, upsert_child_account, upsert_mother_account, upsert_workspace
from app.application.queries.identity import workspaces_query
from app.application.queries.portfolio import portfolio_query
from app.domain.identity import LOCAL_PURPOSE_CHILD, MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED
from app.persistence.database import Base
from app.persistence.models.identity import WorkspaceOfficialMemberSnapshot
from legacy_import.importer import import_legacy_identity
from tests.helpers import make_client
from tests.test_identity import WORKSPACE_UUID, _write_legacy_db


class IdentityQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_lists_imported_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy = tmp_path / "team_manage.db"
            _write_legacy_db(
                legacy,
                teams=[
                    {
                        "email": "owner@icloud.com",
                        "account_id": WORKSPACE_UUID,
                        "team_name": "Team .2026.11",
                        "sub2api_account_id": 10,
                    }
                ],
                children=[
                    {
                        "email": "kid@icloud.com",
                        "status": "active",
                        "current_team_id": 1,
                        "sub2api_account_id": 11,
                    }
                ],
                mappings=[{"team_id": 1, "email": "kid@icloud.com", "status": "joined", "child_account_id": 1}],
            )
            engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'team48.db').as_posix()}")
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with factory() as session:
                await import_legacy_identity(legacy, session)
            await engine.dispose()

            with make_client(tmp_path) as client:
                client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
                overview = client.get("/api/overview").json()
                self.assertEqual(overview["workspaces"], 1)
                self.assertEqual(overview["accounts"], 2)
                workspaces = client.get("/api/workspaces").json()["items"]
                self.assertEqual(workspaces[0]["name"], "Team .2026.11")
                self.assertEqual(workspaces[0]["owner_email"], "owner@icloud.com")
                self.assertEqual(workspaces[0]["official_workspace_id"], WORKSPACE_UUID)
                self.assertIsInstance(workspaces[0]["owner_account_id"], int)
                self.assertIsNone(workspaces[0]["owner_quota_updated_at"])
                self.assertIsNone(workspaces[0]["owner_quota"]["queried_at"])
                accounts = client.get("/api/accounts").json()["items"]
                by_email = {row["email"]: row for row in accounts}
                self.assertEqual(by_email["owner@icloud.com"]["purpose"], "mother")
                self.assertEqual(by_email["owner@icloud.com"]["official_plan"], "unknown")
                self.assertEqual(by_email["kid@icloud.com"]["purpose"], "child")
                audit = client.get("/api/identity/audit").json()
                self.assertEqual(audit["counts"]["verified"], 2)
                portfolio = client.get("/api/accounts/portfolio").json()
                self.assertEqual(len(portfolio["groups"]), 1)
                self.assertFalse(portfolio["usage_available"])
                self.assertIsNone(portfolio["groups"][0]["usage"])

    async def test_stale_invited_membership_follows_official_joined_snapshot(self):

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            mother, _ = await upsert_mother_account(session, email="pioneer_fairway_7m@icloud.com")
            workspace, _ = await upsert_workspace(
                session,
                source_team_id=3,
                official_workspace_id="19df2848-f283-4a98-bb96-861650276d27",
                name="MEXC黑金1",
                subscription_plan=None,
                owner_account_id=mother.id,
                status="active",
                seat_limit=None,
            )
            child, _ = await upsert_child_account(session, email="blimp_digging.1u@icloud.com", status="invited")
            await ensure_membership(
                session,
                workspace_id=workspace.id,
                account_id=child.id,
                official_role="member",
                membership_state=MEMBERSHIP_STATE_INVITED,
                local_purpose=LOCAL_PURPOSE_CHILD,
            )
            now = datetime.utcnow()
            workspace.last_official_sync_at = now
            workspace.last_official_sync_state = "fresh"
            session.add(
                WorkspaceOfficialMemberSnapshot(
                    workspace_id=workspace.id,
                    normalized_email="pioneer_fairway_7m@icloud.com",
                    official_role="owner",
                    remote_state="joined",
                    fetched_at=now,
                )
            )
            session.add(
                WorkspaceOfficialMemberSnapshot(
                    workspace_id=workspace.id,
                    normalized_email="blimp_digging.1u@icloud.com",
                    official_role="member",
                    remote_state="joined",
                    fetched_at=now,
                )
            )
            await session.commit()
            payload = await workspaces_query(session)
            item = payload["items"][0]
            recon = next(row for row in item["reconciliation"]["items"] if row["email"] == "blimp_digging.1u@icloud.com")
            self.assertEqual(recon["status"], "managed")
            self.assertEqual(recon["remote_state"], "joined")
            local = next(row for row in item["managed"]["accounts"] if row["email"] == "blimp_digging.1u@icloud.com")
            self.assertEqual(local["membership_state"], MEMBERSHIP_STATE_JOINED)
            self.assertEqual(local["remote_state"], "joined")
            self.assertEqual(item["official"]["joined_people_total"], 2)
            self.assertEqual(item["reconciliation"]["pending_invites"], 0)
            portfolio = await portfolio_query(session)
            child_card = portfolio["groups"][0]["current_children"][0]
            self.assertEqual(child_card["kind"], "child")
            self.assertEqual(child_card["membership_state"], MEMBERSHIP_STATE_JOINED)
        await engine.dispose()
