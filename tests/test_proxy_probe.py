import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.resources.proxies import proxy_profile_service
from app.application.resources.proxy_probe import ProxyProbeService
from app.persistence.database import Base
from tests.helpers import make_client


class ProxyProbeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.profile = await proxy_profile_service.upsert_from_url(
            self.session,
            "socks5h://user:secret-pass@127.0.0.1:1080",
            name="lab",
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_success_updates_health_not_enabled(self):
        service = ProxyProbeService()
        with (
            patch.object(service, "_request_exit_ip", new=AsyncMock(return_value=("1.2.3.4", 123))),
            patch.object(service, "_request_region", new=AsyncMock(return_value="Los Angeles / CA / US")),
        ):
            result = await service.probe_profile(self.session, self.profile.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["last_exit_ip"], "1.2.3.4")
        self.assertEqual(result["latency_ms"], 123)
        refreshed = await self.session.get(type(self.profile), self.profile.id)
        self.assertEqual(refreshed.health_state, "healthy")
        self.assertEqual(refreshed.status, "active")
        self.assertEqual(refreshed.region, "Los Angeles / CA / US")

    async def test_failure_masks_password_and_keeps_enabled(self):
        service = ProxyProbeService()
        with patch.object(service, "_request_exit_ip", new=AsyncMock(side_effect=RuntimeError("auth failed socks5h://user:secret-pass@127.0.0.1:1080"))):
            result = await service.probe_profile(self.session, self.profile.id)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-pass", result["error"])
        refreshed = await self.session.get(type(self.profile), self.profile.id)
        self.assertEqual(refreshed.health_state, "failed")
        self.assertEqual(refreshed.status, "active")
        self.assertGreaterEqual(refreshed.failure_count, 1)


class ProxyProbeApiTests(unittest.TestCase):
    def test_probe_endpoint_and_get_is_readonly(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            created = client.post("/api/resources/proxies", json={"url": "http://127.0.0.1:8080", "name": "http"}).json()["item"]
            with patch(
                "app.application.resources.proxy_probe.ProxyProbeService._request_exit_ip",
                new=AsyncMock(return_value=("9.9.9.9", 50)),
            ), patch(
                "app.application.resources.proxy_probe.ProxyProbeService._request_region",
                new=AsyncMock(return_value=""),
            ):
                response = client.post(f"/api/resources/proxies/{created['id']}/probe")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertIn("operation_id", payload)
            listed = client.get("/api/resources/proxies").json()["items"]
            self.assertEqual(listed[0]["health_state"], "healthy")
            self.assertNotIn("secret", str(listed))
