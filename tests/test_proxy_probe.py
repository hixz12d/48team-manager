import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.resources.proxies import proxy_profile_service
from app.application.resources.proxy_probe import ProxyProbeService
from app.application.sub2api_proxy_catalog import sub2api_proxy_catalog
from app.persistence.database import Base
from app.persistence.models.resources import ProxyProfile
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

    async def test_remote_catalog_is_safe_and_does_not_mutate_local_profiles(self):
        remote = {
            "id": "7",
            "name": "remote socks5://private-user:secret-pass@proxy.example:1080",
            "protocol": "socks5",
            "host": "proxy.example",
            "port": 1080,
            "status": "active",
            "health_state": "healthy",
            "last_exit_ip": "9.9.9.9",
            "last_checked_at": "2026-09-04T20:00:00Z",
            "region": "US",
            "username": "private-user",
            "password": "secret-pass",
            "url": "socks5://private-user:secret-pass@proxy.example:1080",
        }
        with patch(
            "app.application.sub2api_proxy_catalog.sub2api_client.list_proxies",
            new=AsyncMock(return_value=[remote]),
        ):
            payload = await sub2api_proxy_catalog.list(self.session)

        rows = list((await self.session.execute(select(ProxyProfile))).scalars())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, self.profile.id)
        self.assertEqual(payload["source"], "sub2api")
        self.assertEqual(payload["items"][0]["health"], "healthy")
        self.assertNotIn("secret-pass", str(payload))
        self.assertNotIn("private-user", str(payload))


class ProxyProbeApiTests(unittest.TestCase):
    def test_probe_endpoint_and_get_is_readonly(self):
        remote = {
            "id": 7,
            "name": "remote",
            "protocol": "http",
            "host": "proxy.example",
            "port": 8080,
            "status": "active",
            "health": "healthy",
            "password": "secret-pass",
        }
        probe = AsyncMock(
            return_value={
                "ok": True,
                "health": "healthy",
                "exit_ip": "9.9.9.9",
                "latency_ms": 50,
                "password": "secret-pass",
            }
        )
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            with (
                patch(
                    "app.application.sub2api_proxy_catalog.sub2api_client.list_proxies",
                    new=AsyncMock(return_value=[remote]),
                ),
                patch(
                    "app.application.sub2api_proxy_catalog.sub2api_client.test_proxy",
                    new=probe,
                ),
            ):
                listed_response = client.get("/api/resources/proxies")
                response = client.post("/api/resources/proxies/7/probe")

            self.assertEqual(listed_response.status_code, 200)
            listed = listed_response.json()
            self.assertEqual(listed["source"], "sub2api")
            self.assertEqual(listed["items"][0]["id"], 7)
            self.assertNotIn("secret-pass", str(listed))
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["id"], 7)
            self.assertEqual(payload["exit_ip"], "9.9.9.9")
            self.assertNotIn("secret-pass", str(payload))
            self.assertEqual(probe.await_args.args[1], 7)

            self.assertEqual(
                client.post("/api/resources/proxies", json={"url": "http://127.0.0.1:8080"}).status_code,
                405,
            )
            self.assertEqual(
                client.patch("/api/resources/proxies/7", json={"name": "changed"}).status_code,
                404,
            )
            self.assertEqual(client.get("/api/resources/proxies/7/bindings").status_code, 404)
            self.assertEqual(client.post("/api/resources/proxies/probe-all").status_code, 404)
            self.assertEqual(client.post("/api/resources/proxies/repair").status_code, 404)
            self.assertEqual(client.post("/api/resources/proxies/7/sub2api/sync").status_code, 404)
