"""Isolated local preview with example identities. External/write actions are disabled."""
from __future__ import annotations

from decimal import Decimal
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import gettempdir

from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from app.persistence.models.quota import QuotaSnapshot
from app.persistence.models.identity import ExternalBinding
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot
from app.persistence.models.settings import SystemSetting
from app.application.jobs import scheduler

scheduler.in_test_process = lambda: True
settings = Settings(_env_file=None,
    database_url=f"sqlite+aiosqlite:///{(Path(gettempdir()) / 'team48-preview-unified.db').as_posix()}",
    secret_key="local-preview-only-not-production", admin_username="preview", admin_password="preview-only",
    session_cookie_secure=False, official_quota_probe_enabled=False, auto_reauth_enabled=False,
    auto_rotate_enabled=False, force_refill=False,
)
app = create_app(settings)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def preview_lifespan(app):
    async with original_lifespan(app):
        async with app.state.session_factory() as db:
            if not (await db.execute(select(Account.id).limit(1))).first():
                now = utcnow()
                names = ["North · 研究团队", "West · 创作团队", "Sandbox · 测试团队"]
                emails = [["owner.north@example.com", "member.research@example.com", "member.analysis@example.com"],
                          ["owner.west@example.com", "member.studio@example.com"], ["owner.sandbox@example.com"]]
                index = 0
                for name, members in zip(names, emails):
                    ws = Workspace(name=name, custom_name=name, official_workspace_id=f"00000000-0000-0000-0000-{index + 1:012d}",
                                   last_official_sync_at=now, last_official_sync_state="success", seat_limit=6, occupied_seats=len(members))
                    db.add(ws)
                    await db.flush()
                    for i, email in enumerate(members):
                        index += 1
                        account = Account(email=email, local_purpose="mother" if i == 0 else "child", auth_state="healthy",
                                          operational_state="active", access_token_encrypted="preview-not-a-real-token", credential_revision=1)
                        db.add(account)
                        await db.flush()
                        if i == 0:
                            ws.owner_account_id = account.id
                        binding = ExternalBinding(provider="sub2api", local_account_id=account.id, workspace_id=ws.id,
                                                  remote_account_id=str(index), binding_state="verified", verified_email=email,
                                                  verified_workspace_id=ws.official_workspace_id)
                        db.add(binding)
                        await db.flush()
                        amount = ["12.345", "0", "0.0048312", "6.2", "18.765", "0"][index - 1]
                        db.add(Sub2ApiUsageSnapshot(binding_id=binding.id, local_account_id=account.id, workspace_id=ws.id,
                            remote_account_id=str(index), window_kind="seven_day", window_start_at=now-timedelta(days=7), window_end_at=now,
                            user_cost=Decimal(amount), account_cost=Decimal(amount)*Decimal("0.7"), standard_cost=Decimal(amount),
                            sync_status="success", last_success_at=now, last_attempt_at=now))
                        db.add(WorkspaceMembership(account_id=account.id, workspace_id=ws.id, official_role="owner" if i == 0 or index == 3 else "member",
                                                   membership_state="joined", local_purpose=account.local_purpose))
                        db.add(WorkspaceOfficialMemberSnapshot(workspace_id=ws.id, normalized_email=email, official_role="owner" if i == 0 else "member",
                                                               remote_state="joined", fetched_at=now))
                        db.add(QuotaSnapshot(account_id=account.id, workspace_id=ws.id, source="official", success=True, accepted=True,
                                             credential_revision=1, http_status=200, queried_at=now - timedelta(minutes=58 if index in (2, 3) else index * 3),
                                             five_hour_used_percent=index * 8, seven_day_used_percent=index * 11))
                        if index in (2, 3):
                            db.add(QuotaSnapshot(account_id=account.id, workspace_id=ws.id, source="official", success=False, accepted=True,
                                                 credential_revision=1, http_status=401 if index == 2 else 429, error_code="token_revoked" if index == 2 else "http_429",
                                                 queried_at=now - timedelta(minutes=2)))
                db.add(Account(email="standby.new@example.com", local_purpose="standby", auth_state="unknown", operational_state="available"))
                for key in ("official_quota_probe_enabled", "auth_probe_enabled", "auto_reauth_enabled", "auto_rotate_enabled"):
                    if not await db.get(SystemSetting, key):
                        db.add(SystemSetting(key=key, value="false"))
                await db.commit()
        yield


app.router.lifespan_context = preview_lifespan


@app.middleware("http")
async def preview_write_guard(request, call_next):
    local_reads = {"/api/accounts", "/api/accounts/portfolio", "/api/workspaces", "/api/overview", "/api/quota/runtime"}
    blocked_read = request.url.path.startswith("/api/") and request.url.path not in local_reads
    blocked_write = request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path not in {"/auth/login", "/auth/logout"}
    if blocked_read or blocked_write:
        return JSONResponse({"detail": {"message": "隔离预览不执行账号操作", "error_code": "preview_readonly"}}, status_code=409)
    return await call_next(request)
