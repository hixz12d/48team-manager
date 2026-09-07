import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.queries.portfolio import _quota_risk, portfolio_query
from app.application.sub2api_publish import _build_credentials
from app.core.time import utcnow
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


class SubscriptionEvidenceGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_seat_values_do_not_become_verified_business_tiers(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                owner = Account(email="premium.owner@example.com", local_purpose="mother", official_plan="business")
                db.add(owner)
                await db.flush()
                for raw in ("premium", "standard"):
                    workspace = Workspace(official_workspace_id=f"ws-{raw}", owner_account_id=owner.id, subscription_plan="business")
                    db.add(workspace)
                    await db.flush()
                    db.add(WorkspaceMembership(workspace_id=workspace.id, account_id=owner.id, official_role="owner", membership_state="joined", local_purpose="mother"))
                    db.add(WorkspaceOfficialMemberSnapshot(workspace_id=workspace.id, normalized_email=owner.email, official_role="owner", seat_type=raw, remote_state="joined", fetched_at=utcnow()))
                await db.commit()
                portfolio = await portfolio_query(db)
                for group in portfolio["groups"]:
                    subscription = group["mother"]["subscription"]
                    self.assertEqual(subscription["seat_tier"], "unknown")
                    self.assertIsNone(subscription["observed_at"])
                    self.assertEqual(subscription["workspace_id"], group["id"])
                    self.assertEqual(group["plan_sync"]["reason"], "contract_unverified")
                    self.assertEqual(group["seat_distribution"]["unknown"], 1)
                    self.assertEqual(group["seat_distribution"]["premium"], 0)
        finally:
            await engine.dispose()

    def test_unverified_plan_is_not_written_to_credentials(self):
        for plan in ("unknown", "business", "Business Premium", "business_premium", "free"):
            account = Account(email="member@example.com", official_plan=plan, local_purpose="child")
            self.assertNotIn("plan_type", _build_credentials(account))

    def test_missing_five_hour_quota_is_not_invented_or_full(self):
        self.assertIsNone(_quota_risk({"five_hour_used_percent": None, "seven_day_used_percent": None}))
        self.assertEqual(_quota_risk({"five_hour_used_percent": None, "seven_day_used_percent": 12}), "ok")
