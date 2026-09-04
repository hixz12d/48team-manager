from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.resources.proxies import proxy_profile_service
from app.application.sub2api_proxy import sub2api_proxy_service
from app.application.sub2api_publish import account_sub2api_push
from app.application.sub2api_usage import sub2api_usage_service
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.sub2api import Sub2ApiProxyBinding, Sub2ApiUsageSnapshot


class Sub2ApiManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.account = Account(
            email="billing@example.com",
            official_plan="unknown",
            auth_state="healthy",
            operational_state="active",
            local_purpose="child",
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            official_account_id="acct-billing",
        )
        self.session.add(self.account)
        await self.session.flush()
        self.binding = ExternalBinding(
            provider="sub2api",
            local_account_id=self.account.id,
            remote_account_id="42",
            binding_state="verified",
            verified_email=self.account.email,
            verified_official_account_id=self.account.official_account_id,
        )
        self.session.add(self.binding)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    @staticmethod
    def _billing_payload() -> dict:
        current = {
            "window_stats": {
                "requests": 12,
                "tokens": 3456,
                "standard_cost": "1.2500000000",
                "user_cost": "1.0000000000",
                "cost": "0.7500000000",
            },
            "resets_at": "2026-09-04T20:00:00Z",
        }
        today = {
            "requests": 20,
            "tokens": 5000,
            "standard_cost": "2.0000000000",
            "user_cost": "1.6000000000",
            "cost": "1.1000000000",
        }
        seven_day = {
            "total_requests": 80,
            "total_tokens": 25000,
            "total_standard_cost": "8.0000000000",
            "total_user_cost": "6.5000000000",
            "total_cost": "4.2500000000",
        }
        return {
            "five_hour": {"42": current},
            "today": {"42": today},
            "seven_day": {"42": seven_day},
            "errors": {},
        }

    async def test_usage_sync_persists_all_windows_and_preserves_values_on_failure(self):
        with patch(
            "app.application.sub2api_usage.sub2api_client.fetch_billing_windows",
            new=AsyncMock(return_value=self._billing_payload()),
        ):
            success = await sub2api_usage_service.sync(self.session)

        self.assertTrue(success["ok"])
        self.assertEqual(success["updated_windows"], 3)
        rows = list(
            (
                await self.session.execute(
                    select(Sub2ApiUsageSnapshot).order_by(Sub2ApiUsageSnapshot.window_kind)
                )
            ).scalars()
        )
        self.assertEqual(len(rows), 3)
        five_hour = next(row for row in rows if row.window_kind == "five_hour")
        previous_tokens = five_hour.total_tokens
        previous_success = five_hour.last_success_at

        with patch(
            "app.application.sub2api_usage.sub2api_client.fetch_billing_windows",
            new=AsyncMock(side_effect=RuntimeError("Authorization: Bearer secret-token")),
        ):
            failed = await sub2api_usage_service.sync(self.session)

        self.assertFalse(failed["ok"])
        await self.session.refresh(five_hour)
        self.assertEqual(five_hour.total_tokens, previous_tokens)
        self.assertEqual(five_hour.last_success_at, previous_success)
        self.assertEqual(five_hour.sync_status, "failed")
        self.assertNotIn("secret-token", five_hour.error_message or "")

        payloads = await sub2api_usage_service.payloads_by_context(self.session)
        payload = payloads[(self.account.id, None)]
        self.assertTrue(payload["available"])
        self.assertEqual(payload["windows"]["five_hour"]["tokens"], 3456)
        self.assertEqual(payload["windows"]["five_hour"]["sync_status"], "failed")
        self.assertEqual(payload["windows"]["five_hour"]["billing_margin"], "0.2500000000")

    async def test_usage_status_excludes_unverified_binding_snapshots(self):
        with patch(
            "app.application.sub2api_usage.sub2api_client.fetch_billing_windows",
            new=AsyncMock(return_value=self._billing_payload()),
        ):
            await sub2api_usage_service.sync(self.session)
        self.binding.binding_state = "conflict"
        await self.session.commit()

        status = await sub2api_usage_service.status(self.session)

        self.assertEqual(status["verified_bindings"], 0)
        self.assertEqual(status["snapshot_count"], 0)
        self.assertIsNone(status["last_success_at"])

    async def test_proxy_sync_is_idempotent_and_recreates_missing_remote(self):
        profile = await proxy_profile_service.upsert_from_url(
            self.session,
            "socks5://user:password@127.0.0.1:1080",
            name="billing-proxy",
        )
        await self.session.commit()
        create = AsyncMock(return_value={"id": 9})
        update = AsyncMock(return_value={"id": 9})
        get = AsyncMock(return_value={"id": 9})

        with (
            patch("app.application.sub2api_proxy.sub2api_client.create_proxy", new=create),
            patch("app.application.sub2api_proxy.sub2api_client.update_proxy", new=update),
            patch("app.application.sub2api_proxy.sub2api_client.get_proxy", new=get),
        ):
            first = await sub2api_proxy_service.sync_profile(self.session, profile.id)
            second = await sub2api_proxy_service.sync_profile(self.session, profile.id)

        self.assertEqual(first["action"], "create")
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(create.await_count, 1)
        update.assert_not_awaited()
        sent = create.await_args.args[1]
        self.assertEqual(sent["protocol"], "socks5h")
        self.assertEqual(sent["username"], "user")
        self.assertEqual(sent["password"], "password")
        self.assertTrue(create.await_args.kwargs["idempotency_key"].startswith("team48-proxy-"))

        request = httpx.Request("GET", "https://sub2api.example/api/v1/admin/proxies/9")
        response = httpx.Response(404, request=request)
        missing = httpx.HTTPStatusError("not found", request=request, response=response)
        recreate = AsyncMock(return_value={"id": 10})
        with (
            patch(
                "app.application.sub2api_proxy.sub2api_client.get_proxy",
                new=AsyncMock(side_effect=missing),
            ),
            patch("app.application.sub2api_proxy.sub2api_client.create_proxy", new=recreate),
        ):
            rebuilt = await sub2api_proxy_service.sync_profile(self.session, profile.id)

        self.assertEqual(rebuilt["action"], "recreate")
        self.assertEqual(rebuilt["remote_proxy_id"], 10)
        mapping = (
            await self.session.execute(
                select(Sub2ApiProxyBinding).where(
                    Sub2ApiProxyBinding.local_proxy_profile_id == profile.id
                )
            )
        ).scalar_one()
        self.assertEqual(mapping.remote_proxy_id, "10")

    async def test_existing_account_update_only_sends_explicit_fields(self):
        profile = await proxy_profile_service.upsert_from_url(
            self.session, "http://127.0.0.1:8080", name="local-only"
        )
        self.account.proxy_profile_id = profile.id
        self.account.proxy = "http://127.0.0.1:8080"
        await self.session.commit()
        remote = {
            "id": 42,
            "credentials": {
                "email": self.account.email,
                "chatgpt_account_id": self.account.official_account_id,
            },
            "extra": {"email": self.account.email},
        }
        update = AsyncMock(return_value={"id": 42})
        schedulable = AsyncMock(return_value={"patched": True})
        resolve_proxy = AsyncMock()
        with (
            patch(
                "app.application.sub2api_publish.decrypt_secret",
                side_effect=lambda value: "decrypted" if value else "",
            ),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=update),
            patch(
                "app.application.sub2api_publish.sub2api_client.read_after_write",
                new=AsyncMock(return_value=remote),
            ),
            patch(
                "app.application.sub2api_publish.sub2api_client.set_account_schedulable",
                new=schedulable,
            ),
            patch(
                "app.application.sub2api_publish.sub2api_proxy_service.resolve_for_push",
                new=resolve_proxy,
            ),
        ):
            preview = await account_sub2api_push(
                self.session, self.account.id, dry_run=True
            )
            result = await account_sub2api_push(self.session, self.account.id)

        self.assertTrue(result["ok"])
        self.assertEqual(preview["proxy"]["source"], "preserve")
        self.assertNotIn("proxy_id", preview["would_update"])
        sent = update.await_args.args[2]
        self.assertEqual(set(sent), {"credentials"})
        for field in ("concurrency", "priority", "extra", "group_ids", "proxy_id"):
            self.assertNotIn(field, sent)
        schedulable.assert_not_awaited()
        resolve_proxy.assert_not_awaited()
        self.assertIn("concurrency", result["preserved_fields"])
        self.assertIn("priority", result["preserved_fields"])
        self.assertIn("extra", result["preserved_fields"])

    async def test_template_reapply_requires_template_id(self):
        result = await account_sub2api_push(
            self.session, self.account.id, reapply_template=True, dry_run=True
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "template_id_required")

    async def test_unsupported_template_contract_never_writes_remote(self):
        update = AsyncMock()
        with (
            patch(
                "app.application.sub2api_publish.sub2api_client.integration_capabilities",
                new=AsyncMock(return_value={"account_templates": {"apply": False}}),
            ),
            patch(
                "app.application.sub2api_publish.sub2api_client.update_account",
                new=update,
            ),
        ):
            result = await account_sub2api_push(
                self.session,
                self.account.id,
                template_id="template-1",
                reapply_template=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "account_templates_unsupported")
        update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
