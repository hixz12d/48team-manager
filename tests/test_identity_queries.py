import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.persistence.database import Base
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
                accounts = client.get("/api/accounts").json()["items"]
                by_email = {row["email"]: row for row in accounts}
                self.assertEqual(by_email["owner@icloud.com"]["purpose"], "mother")
                self.assertEqual(by_email["owner@icloud.com"]["official_plan"], "unknown")
                self.assertEqual(by_email["kid@icloud.com"]["purpose"], "child")
                audit = client.get("/api/identity/audit").json()
                self.assertEqual(audit["counts"]["verified"], 2)
