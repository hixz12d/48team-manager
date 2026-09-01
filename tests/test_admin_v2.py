import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.models import Account, QuotaSnapshot, Workspace, WorkspaceMembership
from app.services.admin_v2 import AccountVersionConflict, admin_v2_service
from app.services.operations import operation_store


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


class AdminV2JsContractTests(unittest.TestCase):
    def test_frontend_uses_per_entity_abort_and_row_ids(self):
        root = os.path.dirname(os.path.dirname(__file__))
        js_path = os.path.join(root, "app", "static", "js", "admin_v2.js")
        with open(js_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("const activeRequests = new Map()", source)
        self.assertIn("activeRequests.get(key)?.abort()", source)
        self.assertIn("id=\"account-${account.id}\"", source)
        self.assertIn("row.outerHTML = accountRowHtml(account)", source)
        self.assertIn("document.getElementById(`account-${accountId}`)?.remove()", source)
        self.assertNotIn("refreshTeamDashboard", source)
        self.assertNotIn("innerHTML = board", source)


class AdminV2BoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed(self):
        mother = Account(
            email="owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="healthy",
            version=1,
        )
        child = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
            version=3,
        )
        other = Account(
            email="other@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="healthy",
            version=1,
        )
        self.session.add_all([mother, child, other])
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            name="Team Alpha",
            owner_account_id=mother.id,
            status="active",
            seat_limit=5,
            version=1,
        )
        self.session.add(workspace)
        await self.session.flush()
        self.session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    account_id=mother.id,
                    official_role="owner",
                    membership_state="joined",
                    local_purpose="mother",
                ),
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    account_id=child.id,
                    official_role="member",
                    membership_state="joined",
                    local_purpose="child",
                ),
            ]
        )
        self.session.add(
            QuotaSnapshot(
                account_id=child.id,
                five_hour_used_percent=10,
                seven_day_used_percent=100,
                source="official",
                queried_at=datetime(2026, 3, 29, 12, 0, 0),
                success=True,
            )
        )
        await self.session.commit()
        return mother, child, other, workspace

    async def test_board_groups_accounts_and_lists_operations(self):
        _mother, child, _other, workspace = await self._seed()
        op = await operation_store.create(
            self.session,
            op_type="reauth",
            team_id=workspace.id,
            email=child.email,
            input_payload={"email": child.email},
        )
        await operation_store.mark_step(self.session, op, "browser", state="running")
        await self.session.commit()
        board = await admin_v2_service.build_board(self.session)
        self.assertEqual(board["overview"]["needs_auth"], 1)
        self.assertEqual(board["overview"]["weekly_full"], 1)
        names = [group["name"] for group in board["workspaces"]]
        self.assertIn("Team Alpha", names)
        alpha = next(group for group in board["workspaces"] if group["name"] == "Team Alpha")
        emails = [item["email"] for item in alpha["accounts"]]
        self.assertEqual(emails, ["owner@icloud.com", "kid@icloud.com"])
        self.assertEqual(board["operations"][0]["id"], op.public_id)
        self.assertEqual(board["operations"][0]["steps"][0]["step_name"], "browser")
        self.assertEqual(board["operations"][0].get("input"), {})

    async def test_patch_one_account_does_not_touch_another(self):
        _mother, child, other, _workspace = await self._seed()
        payload = await admin_v2_service.patch_account(
            self.session,
            child.id,
            version=3,
            operational_state="standby",
        )
        self.assertEqual(payload["id"], child.id)
        self.assertEqual(payload["operational_state"], "standby")
        self.assertEqual(payload["version"], 4)
        refreshed_other = await self.session.get(Account, other.id)
        self.assertEqual(refreshed_other.operational_state, "active")
        self.assertEqual(refreshed_other.version, 1)

    async def test_stale_version_raises_conflict(self):
        _mother, child, _other, _workspace = await self._seed()
        with self.assertRaises(AccountVersionConflict):
            await admin_v2_service.patch_account(self.session, child.id, version=1, operational_state="standby")
        refreshed = await self.session.get(Account, child.id)
        self.assertEqual(refreshed.operational_state, "active")
        self.assertEqual(refreshed.version, 3)

    async def test_archive_only_marks_one_row(self):
        _mother, child, other, _workspace = await self._seed()
        payload = await admin_v2_service.archive_account(self.session, child.id, version=3)
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["operational_state"], "archived")
        leftover = await self.session.get(Account, other.id)
        self.assertEqual(leftover.operational_state, "active")

    async def test_refresh_official_only_probes_one_account(self):
        _mother, child, other, _workspace = await self._seed()
        probed = []

        async def fake_probe(session, account, now=None):
            probed.append(account.id)
            session.add(
                QuotaSnapshot(
                    account_id=account.id,
                    five_hour_used_percent=1,
                    seven_day_used_percent=2,
                    source="official",
                    queried_at=datetime(2026, 3, 29, 13, 0, 0),
                    success=True,
                )
            )

        with patch("app.services.admin_v2.quota_service.probe_account", new=fake_probe):
            payload = await admin_v2_service.refresh_official(self.session, child.id)
        self.assertEqual(probed, [child.id])
        self.assertEqual(payload["quota"]["seven_day_used_percent"], 2)
        other_quota = await admin_v2_service.get_account(self.session, other.id)
        self.assertIsNone(other_quota["quota"])

    async def test_settings_mask_secrets_and_never_force_refill(self):
        from app.models import Setting

        self.session.add_all(
            [
                Setting(key="sub2api_api_key", value="super-secret-key"),
                Setting(key="hme_service_token", value="hme-secret"),
            ]
        )
        await self.session.commit()
        shown = await admin_v2_service.get_settings(self.session)
        self.assertNotIn("super-secret-key", shown["sub2api_api_key"])
        self.assertTrue(shown["sub2api_api_key_set"])
        self.assertFalse(shown["auto_rotate_force_refill"])
        with patch.object(admin_v2_service, "_apply_job_updates"):
            updated = await admin_v2_service.update_settings(
                self.session,
                {"auto_rotate_enabled": True, "auto_rotate_force_refill": True},
            )
        self.assertFalse(updated["auto_rotate_force_refill"])


class AdminV2RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_patch_returns_409(self):
        from app.routes import admin_v2

        async def boom(*args, **kwargs):
            raise AccountVersionConflict(9, 4)

        with patch.object(admin_v2.admin_v2_service, "patch_account", new=boom):
            response = await admin_v2.v2_patch_account(
                9,
                admin_v2.AccountPatchRequest(version=3, operational_state="standby"),
                db=object(),
                current_user={"username": "admin", "is_admin": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.body.decode("utf-8").find("version_conflict") > 0, True)


class AdminV2MigrationSmokeTests(unittest.TestCase):
    def test_v2_does_not_drop_legacy_tables(self):
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
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()
            self.assertIn("teams", tables)
            self.assertIn("accounts", tables)
            self.assertIn("operations", tables)


if __name__ == "__main__":
    unittest.main()
