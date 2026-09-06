"""Durable quota context schedules and version-guarded, short-transaction probes."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.settings import as_bool, get_setting_value
from app.core.config import load_settings
from app.core.crypto import token_cipher
from app.core.time import as_utc, isoformat, utcnow
from app.domain.quota import (
    DEFAULT_QUOTA_PROBE_BATCH_SIZE, DEFAULT_QUOTA_PROBE_ENABLED,
    DEFAULT_QUOTA_PROBE_STAGGER_MINUTES, MAX_QUOTA_PROBE_BATCH_SIZE,
    MIN_QUOTA_PROBE_BATCH_SIZE, SKIP_OPERATIONAL_STATES, SOURCE_OFFICIAL,
    QuotaResult, failure_next_quota_probe_at, initial_next_quota_probe_at,
    success_next_quota_probe_at,
)
from app.domain.quota_health import AUTH_CODES, LABELS, present_context, result_state
from app.integrations.openai.quota import OpenAIQuotaClient
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.quota import QuotaSnapshot, QuotaProbeState, ProbeDispatchLease

logger = logging.getLogger(__name__)
LEASE_SECONDS = 360


def clamp_batch_size(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_QUOTA_PROBE_BATCH_SIZE
    return max(MIN_QUOTA_PROBE_BATCH_SIZE, min(MAX_QUOTA_PROBE_BATCH_SIZE, number))


def decrypt_access_token(account: Account) -> str | None:
    if not account.access_token_encrypted:
        return None
    try:
        return str(token_cipher().decrypt(account.access_token_encrypted) or "").strip() or None
    except Exception:
        return None


def resolve_chatgpt_account_id(account: Account, workspace: Workspace | None = None) -> str | None:
    if workspace is not None:
        # An explicit workspace must never silently fall back to another context.
        return str(workspace.official_workspace_id or "").strip() or None
    return str(account.official_account_id or "").strip() or None


def context_key(account_id, workspace_id):
    return f"{int(account_id)}:{int(workspace_id) if workspace_id is not None else 'none'}"


def snapshot_from_result(account_id, result, now, *, workspace_id=None):
    return QuotaSnapshot(
        account_id=account_id, workspace_id=workspace_id,
        five_hour_used_percent=result.five_hour_used_percent, five_hour_reset_at=result.five_hour_reset_at,
        seven_day_used_percent=result.seven_day_used_percent, seven_day_reset_at=result.seven_day_reset_at,
        source=result.source or SOURCE_OFFICIAL, queried_at=result.queried_at or now,
        success=bool(result.success), error_code=result.error_code,
        error_message=LABELS[result_state(result)][0] if not result.success else None,
        http_status=result.http_status, error_source=result.error_source, request_count=result.request_count,
        credential_revision=result.credential_revision, check_id=result.check_id,
        started_at=result.started_at, retry_after_at=result.retry_after_at, accepted=True, created_at=now,
    )


def serialize_snapshot(row):
    return {key: getattr(row, key) for key in (
        "id", "account_id", "workspace_id", "five_hour_used_percent", "five_hour_reset_at",
        "seven_day_used_percent", "seven_day_reset_at", "source", "queried_at", "success",
        "error_code", "error_message", "http_status", "credential_revision", "check_id", "accepted",
    )}


class QuotaService:
    def __init__(self, client: OpenAIQuotaClient | None = None):
        self.client = client or OpenAIQuotaClient()

    async def load_settings(self, db):
        env = load_settings()
        # Explicit persisted setting > environment > safe default. No new automation opt-in.
        enabled_raw = await get_setting_value(db, "official_quota_probe_enabled", str(bool(env.official_quota_probe_enabled)).lower())
        enabled = as_bool(enabled_raw, DEFAULT_QUOTA_PROBE_ENABLED)
        stagger = await get_setting_value(db, "official_quota_probe_stagger_minutes", str(DEFAULT_QUOTA_PROBE_STAGGER_MINUTES))
        batch = await get_setting_value(db, "official_quota_probe_batch_size", str(DEFAULT_QUOTA_PROBE_BATCH_SIZE))
        try:
            stagger = max(1, min(1440, int(stagger)))
        except (ValueError, TypeError):
            stagger = DEFAULT_QUOTA_PROBE_STAGGER_MINUTES
        return {"enabled": enabled, "effective_enabled": enabled,
                "disabled_reason": None if enabled else "额度定时检测未开启",
                "interval_minutes": 60, "stagger_minutes": stagger, "batch_size": clamp_batch_size(batch)}

    async def _latest(self, db, *, by_context=True, success_only=False, authority=False):
        filters = [QuotaSnapshot.source == SOURCE_OFFICIAL, QuotaSnapshot.accepted.is_(True)]
        if success_only:
            filters.append(QuotaSnapshot.success.is_(True))
        if authority:
            filters.extend([
                QuotaSnapshot.credential_revision == Account.credential_revision,
                or_(QuotaSnapshot.error_source.is_(None), QuotaSnapshot.error_source == "official_quota"),
                or_(QuotaSnapshot.success.is_(True), QuotaSnapshot.http_status == 401,
                    QuotaSnapshot.error_code.in_(AUTH_CODES)),
            ])
        partition = [QuotaSnapshot.account_id]
        if by_context:
            partition.append(QuotaSnapshot.workspace_id)
        ranked = select(QuotaSnapshot.id, func.row_number().over(
            partition_by=partition,
            order_by=(func.coalesce(QuotaSnapshot.started_at, QuotaSnapshot.queried_at).desc(), QuotaSnapshot.id.desc()),
        ).label("rank")).where(*filters)
        if authority:
            ranked = ranked.join(Account, Account.id == QuotaSnapshot.account_id)
        ranked = ranked.subquery()
        rows = (await db.execute(select(QuotaSnapshot).join(ranked, ranked.c.id == QuotaSnapshot.id).where(ranked.c.rank == 1))).scalars()
        return {(r.account_id, r.workspace_id) if by_context else r.account_id: r for r in rows}

    async def latest_official(self, db, account_id):
        account = await db.get(Account, account_id)
        if account is None:
            return None
        from app.domain.identity.binding import AmbiguousWorkspaceContext
        try:
            workspace = await self.resolve_probe_workspace(db, account)
        except AmbiguousWorkspaceContext:
            return None
        return (await db.execute(select(QuotaSnapshot).where(
            QuotaSnapshot.account_id == account_id,
            QuotaSnapshot.workspace_id == (workspace.id if workspace else None),
            QuotaSnapshot.source == SOURCE_OFFICIAL, QuotaSnapshot.accepted.is_(True),
            QuotaSnapshot.credential_revision == account.credential_revision,
            QuotaSnapshot.queried_at >= utcnow() - timedelta(minutes=75),
        ).order_by(QuotaSnapshot.started_at.desc(), QuotaSnapshot.id.desc()).limit(1))).scalar_one_or_none()

    async def latest_official_by_accounts(self, db, *, success_only=False):
        return await self._latest(db, by_context=False, success_only=success_only)

    async def latest_official_by_contexts(self, db, *, success_only=False):
        return await self._latest(db, success_only=success_only)

    async def health_reader(self, db):
        latest = await self._latest(db)
        successes = await self._latest(db, success_only=True)
        authority = await self._latest(db, authority=True)
        schedules = { (r.account_id, r.workspace_id): r for r in (await db.execute(select(QuotaProbeState))).scalars() }
        def read(account, workspace_id):
            key = (account.id, workspace_id)
            return present_context(account, latest.get(key), successes.get(key), authority.get(key), schedules.get(key))
        return read

    async def resolve_probe_workspace(self, db, account, workspace_id=None):
        from app.domain.identity.binding import resolve_workspace_context
        memberships = list((await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id))).scalars())
        workspaces = {r.id: r for r in (await db.execute(select(Workspace))).scalars()}
        return resolve_workspace_context(account, memberships=memberships, workspaces_by_id=workspaces, workspace_id=workspace_id)

    async def active_contexts(self, db):
        accounts = list((await db.execute(select(Account).where(Account.operational_state.not_in(SKIP_OPERATIONAL_STATES)))).scalars())
        workspaces = {r.id: r for r in (await db.execute(select(Workspace).where(Workspace.status.not_in(SKIP_OPERATIONAL_STATES)))).scalars()}
        memberships = list((await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.membership_state == "joined"))).scalars())
        result = []
        for account in accounts:
            ids = {r.workspace_id for r in memberships if r.account_id == account.id and r.workspace_id in workspaces}
            ids.update(ws.id for ws in workspaces.values() if ws.owner_account_id == account.id)
            # Invited/history-only identities are not an eligible personal context.
            if not ids:
                has_relation = (await db.execute(select(WorkspaceMembership.id).where(WorkspaceMembership.account_id == account.id).limit(1))).first()
                if has_relation:
                    continue
            for ws_id in sorted(ids) if ids else [None]:
                result.append((account, workspaces.get(ws_id)))
        return result

    async def ensure_state(self, db, account, workspace_id, *, now, immediate=False, stagger_minutes=60):
        key = context_key(account.id, workspace_id)
        await db.execute(insert(QuotaProbeState).values(
            context_key=key, account_id=account.id, workspace_id=workspace_id,
            credential_revision=int(account.credential_revision or 1), fail_count=0, sequence=0, enabled=True,
            next_check_at=now if immediate else initial_next_quota_probe_at(account.id, now, stagger_minutes),
        ).on_conflict_do_nothing(index_elements=["context_key"]))
        state = await db.get(QuotaProbeState, key, populate_existing=True)
        return state

    async def enqueue(self, db, account, workspace_id=None, *, source="manual", now=None):
        from app.application.operations import operation_store
        stamp = now or utcnow()
        workspace = await self.resolve_probe_workspace(db, account, workspace_id)
        ws_id = workspace.id if workspace else None
        if account.operational_state in SKIP_OPERATIONAL_STATES or account.auth_state == "deactivated":
            return {"ok": False, "error_code": "account_disabled", "error": "该账号已停用或归档"}
        state = await self.ensure_state(db, account, ws_id, now=stamp, immediate=True)
        if state.operation_id:
            active = await operation_store.get_by_public_id(db, state.operation_id)
            if active and active.state in {"queued", "running", "waiting"}:
                await db.commit()
                return {"ok": True, "success": True, "status": active.state, "operation_id": active.public_id, "reused_existing": True}
        operation = await operation_store.create(db, op_type="quota_probe", account_id=account.id,
            workspace_id=ws_id, email=account.email, state="queued", source=source,
            input_payload={"account_id": account.id, "workspace_id": ws_id}, now=stamp)
        state.operation_id = operation.public_id
        state.enabled = True
        # Manual requests do not bypass a server-provided rate-limit deadline.
        latest = (await db.execute(select(QuotaSnapshot).where(
            QuotaSnapshot.account_id == account.id, QuotaSnapshot.workspace_id == ws_id,
            QuotaSnapshot.accepted.is_(True)).order_by(QuotaSnapshot.id.desc()).limit(1))).scalar_one_or_none()
        retry_at = as_utc(latest.retry_after_at) if latest and latest.credential_revision == account.credential_revision else None
        state.next_check_at = max(as_utc(stamp), retry_at) if retry_at else stamp
        public_id = operation.public_id
        await db.commit()
        return {"ok": True, "success": True, "status": "queued", "operation_id": public_id, "account_id": account.id, "workspace_id": ws_id}

    async def enqueue_after_credentials(self, db, account):
        memberships = list((await db.execute(select(WorkspaceMembership).where(
            WorkspaceMembership.account_id == account.id, WorkspaceMembership.membership_state == "joined"))).scalars())
        workspaces = list((await db.execute(select(Workspace).where(Workspace.owner_account_id == account.id))).scalars())
        ids = {row.workspace_id for row in memberships} | {row.id for row in workspaces}
        results = []
        for ws_id in sorted(ids) if ids else [None]:
            results.append(await self.enqueue(db, account, ws_id, source="credential_update"))
        return results

    async def enqueue_all(self, db):
        ids = []
        for account, workspace in await self.active_contexts(db):
            result = await self.enqueue(db, account, workspace.id if workspace else None)
            if result.get("operation_id"):
                ids.append(result["operation_id"])
        return {"ok": True, "success": True, "status": "queued", "operation_ids": ids, "queued": len(ids)}

    async def run_probe_once(self, db, *, now=None, settings=None, force=False):
        cfg = settings or await self.load_settings(db)
        stamp = now or utcnow()
        stats = {"enabled": bool(cfg.get("enabled")), "scanned": 0, "due": 0, "probed": 0, "failed": 0, "skipped": 0, "account_ids": []}
        if not cfg.get("enabled") and not force:
            stats["skipped"] = 1
            return stats
        contexts = await self.active_contexts(db)
        keys = []
        for account, ws in contexts:
            state = await self.ensure_state(db, account, ws.id if ws else None, now=stamp, stagger_minutes=cfg.get("stagger_minutes", 60))
            keys.append(state.context_key)
            state.enabled = bool(account.access_token_encrypted) and account.auth_state != "deactivated"
            if state.credential_revision != int(account.credential_revision or 1):
                state.next_check_at = stamp
                state.fail_count = 0
                state.credential_revision = int(account.credential_revision or 1)
            # Honor the pre-migration schedule on initial creation.
            if state.last_attempt_at is None and account.next_quota_probe_at is not None:
                state.next_check_at = account.next_quota_probe_at
        await db.execute(update(QuotaProbeState).execution_options(synchronize_session="fetch").where(QuotaProbeState.context_key.not_in(keys)).values(enabled=False))
        await db.commit()
        stats["scanned"] = len(contexts)
        due = list((await db.execute(select(QuotaProbeState).where(
            QuotaProbeState.enabled.is_(True), QuotaProbeState.next_check_at <= stamp,
            QuotaProbeState.operation_id.is_(None),
        ).order_by(QuotaProbeState.next_check_at, QuotaProbeState.context_key).limit(clamp_batch_size(cfg.get("batch_size"))))).scalars())
        stats["due"] = len(due)
        for state in due:
            account = await db.get(Account, state.account_id)
            result = await self.enqueue(db, account, state.workspace_id, source="scheduled", now=stamp)
            if result.get("operation_id"):
                stats["account_ids"].append(account.id)
        executed = await self.run_queued_once(db, now=now, limit=clamp_batch_size(cfg.get("batch_size")))
        stats.update(probed=executed["probed"], failed=executed["failed"])
        return stats

    async def run_queued_once(self, db, *, now=None, limit=1):
        stamp = now or utcnow()
        ticket = uuid.uuid4().hex
        await db.execute(insert(ProbeDispatchLease).values(name="official").on_conflict_do_nothing())
        claimed = await db.execute(update(ProbeDispatchLease).execution_options(synchronize_session="fetch").where(
            ProbeDispatchLease.name == "official",
            or_(ProbeDispatchLease.expires_at.is_(None), ProbeDispatchLease.expires_at <= stamp),
            or_(ProbeDispatchLease.next_request_at.is_(None), ProbeDispatchLease.next_request_at <= stamp),
        ).values(token=ticket, expires_at=stamp + timedelta(seconds=LEASE_SECONDS)))
        await db.commit()
        if claimed.rowcount != 1:
            return {"probed": 0, "failed": 0}
        result = {"probed": 0, "failed": 0}
        try:
            result = await self._run_queued_claimed(db, now=now, limit=1)
            return result
        finally:
            await db.rollback()
            values = {"token": None, "expires_at": None}
            if result["probed"] or result["failed"]:
                values["next_request_at"] = (now or utcnow()) + timedelta(seconds=20)
            await db.execute(update(ProbeDispatchLease).execution_options(synchronize_session="fetch").where(ProbeDispatchLease.name == "official",
                ProbeDispatchLease.token == ticket).values(**values))
            await db.commit()

    async def _run_queued_claimed(self, db, *, now=None, limit=1):
        from app.application.operations import operation_store
        stamp = now or utcnow()
        stats = {"probed": 0, "failed": 0}
        states = list((await db.execute(select(QuotaProbeState).where(
            QuotaProbeState.operation_id.is_not(None), QuotaProbeState.next_check_at <= stamp,
            or_(QuotaProbeState.lease_expires_at.is_(None), QuotaProbeState.lease_expires_at <= stamp),
        ).order_by(QuotaProbeState.next_check_at, QuotaProbeState.context_key).limit(limit))).scalars())
        for state in states:
            operation = await operation_store.get_by_public_id(db, state.operation_id)
            if operation is None or operation.state not in {"queued", "running", "waiting"}:
                state.operation_id = None
                await db.commit()
                continue
            account = await db.get(Account, state.account_id)
            if operation.cancel_requested or account is None or account.operational_state in SKIP_OPERATIONAL_STATES or account.auth_state == "deactivated":
                await operation_store.finish(db, operation, {"success": False, "status": "cancelled", "error_code": "cancelled"})
                state.operation_id = None
                await db.commit()
                continue
            operation.state = "running"
            operation.started_at = stamp
            operation.lease_expires_at = stamp + timedelta(seconds=LEASE_SECONDS)
            await operation_store.mark_step(db, operation, "quota_probe", state="running")
            await db.commit()
            try:
                result = await self.probe_account(db, account, workspace_id=state.workspace_id, now=now)
            except ValueError:
                result = QuotaResult(False, error_code="invalid_workspace_context", error_message="工作区关系已变化", error_source="local")
            if result.error_code == "already_running":
                continue
            await db.refresh(operation)
            if operation.cancel_requested:
                result = QuotaResult(False, error_code="cancelled", error_message="任务已取消")
            await operation_store.mark_step(db, operation, "quota_probe", state="success" if result.success else "failed", error_code=result.error_code)
            await operation_store.finish(db, operation, {"success": result.success, "status": "cancelled" if operation.cancel_requested else ("success" if result.success else "failed"),
                "account_id": account.id, "workspace_id": state.workspace_id, "error_code": result.error_code,
                "error": result.error_message, "http_status": result.http_status})
            await db.execute(update(QuotaProbeState).execution_options(synchronize_session="fetch").where(QuotaProbeState.context_key == state.context_key,
                QuotaProbeState.operation_id == operation.public_id).values(operation_id=None))
            await db.commit()
            stats["probed" if result.success else "failed"] += 1
        return stats

    async def probe_account(self, db, account, *, now=None, workspace_id=None, workspace=None):
        stamp = now or utcnow()
        if account.operational_state in SKIP_OPERATIONAL_STATES or account.auth_state == "deactivated":
            return QuotaResult(False, error_code="account_disabled", error_source="local", queried_at=stamp)
        if workspace is None:
            workspace = await self.resolve_probe_workspace(db, account, workspace_id)
        ws_id = workspace.id if workspace else None
        state = await self.ensure_state(db, account, ws_id, now=stamp)
        key = state.context_key
        ticket = uuid.uuid4().hex
        revision = int(account.credential_revision or 1)
        claimed = await db.execute(update(QuotaProbeState).execution_options(synchronize_session="fetch").where(QuotaProbeState.context_key == key,
            or_(QuotaProbeState.lease_expires_at.is_(None), QuotaProbeState.lease_expires_at <= stamp)).values(
                lease_token=ticket, lease_expires_at=stamp + timedelta(seconds=LEASE_SECONDS),
                sequence=QuotaProbeState.sequence + 1, last_attempt_at=stamp))
        if claimed.rowcount != 1:
            await db.commit()
            return QuotaResult(success=False, error_code="already_running", error_message="检测已在运行", queried_at=stamp)
        token = decrypt_access_token(account)
        official_id = resolve_chatgpt_account_id(account, workspace)
        identifier = account.email or "default"
        account_id = account.id
        has_ciphertext = bool(account.access_token_encrypted)
        await db.commit()
        # No database write transaction spans the external request.
        initial_recorded = False
        try:
            if not token:
                result = QuotaResult(success=False, error_code="credential_error" if has_ciphertext else "missing_token", error_source="local")
            elif workspace is not None and not official_id:
                result = QuotaResult(success=False, error_code="workspace_id_missing", error_source="local")
            else:
                result = await asyncio.wait_for(self.client.fetch_quota(access_token=token, db_session=db,
                    workspace_id=official_id, identifier=identifier, now=now), timeout=90)
        except Exception:
            result = QuotaResult(success=False, error_code="transport", error_source="official_quota")
        if result.http_status == 401 and account.refresh_token_encrypted:
            from app.application.tokens import auth_service
            cfg = await auth_service.load_settings(db)
            if cfg["enabled"]:
                result.queried_at = now or utcnow()
                result.started_at = stamp
                result.credential_revision = revision
                result.check_id = uuid.uuid4().hex
                await db.commit()
                await db.execute(update(QuotaProbeState).execution_options(synchronize_session="fetch").where(QuotaProbeState.context_key == key).values(context_key=key))
                current = (await db.execute(select(Account.credential_revision).where(Account.id == account_id))).scalar_one()
                initial = snapshot_from_result(account_id, result, now or utcnow(), workspace_id=ws_id)
                initial.accepted = current == revision
                db.add(initial)
                await db.commit()
                initial_recorded = True
                refreshed = await auth_service.refresh_account(db, account, now=now, schedule_checks=False) if current == revision else {}
                if refreshed.get("success"):
                    initial_recorded = False
                    revision = int(account.credential_revision or 1)
                    stamp = now or utcnow()
                    try:
                        result = await asyncio.wait_for(self.client.fetch_quota(access_token=decrypt_access_token(account),
                            db_session=db, workspace_id=official_id, identifier=identifier, now=now), timeout=90)
                    except Exception:
                        result = QuotaResult(success=False, error_code="transport")
        completed = now or utcnow()
        result.queried_at = completed
        result.started_at = stamp
        result.credential_revision = revision
        result.check_id = ticket
        result.error_message = LABELS[result_state(result)][0] if not result.success else None
        # Release read transactions created by proxy resolution before the atomic result check.
        await db.commit()
        await db.execute(update(QuotaProbeState).execution_options(synchronize_session="fetch").where(QuotaProbeState.context_key == key).values(context_key=key))
        current_revision = (await db.execute(select(Account.credential_revision).where(Account.id == account_id))).scalar_one_or_none()
        current_state = await db.get(QuotaProbeState, key, populate_existing=True)
        accepted = current_revision == revision and current_state is not None and current_state.lease_token == ticket
        snapshot = snapshot_from_result(account_id, result, completed, workspace_id=ws_id)
        snapshot.accepted = accepted
        if not initial_recorded:
            db.add(snapshot)
        elif not accepted:
            initial.accepted = False
        if accepted:
            count = 0 if result.success else int(current_state.fail_count or 0) + 1
            next_at = success_next_quota_probe_at(completed, 0) if result.success else failure_next_quota_probe_at(completed, count)
            if result_state(result) in {"auth_required", "unauthorized", "credential_error", "deactivated"}:
                next_at = None
            elif not result.success:
                authority = (await self._latest(db, authority=True)).get((account_id, ws_id))
                if authority is not None and result_state(authority) == "auth_required":
                    next_at = None
            if result.retry_after_at:
                next_at = max(as_utc(next_at or completed), as_utc(result.retry_after_at))
            current_state.next_check_at = next_at
            current_state.fail_count = count
            current_state.credential_revision = revision
        if current_state is not None and current_state.lease_token == ticket:
            current_state.lease_token = None
            current_state.lease_expires_at = None
        await db.commit()
        if not accepted:
            result.success = False
            result.error_code = "superseded"
            result.error_message = "凭证或检查版本已变化，旧结果未生效"
        return result

    async def runtime_summary(self, db):
        cfg = await self.load_settings(db)
        now = utcnow()
        rows = list((await db.execute(select(QuotaProbeState).where(QuotaProbeState.enabled.is_(True)))).scalars())
        overdue = [max(0, (now - as_utc(r.next_check_at)).total_seconds()) for r in rows if r.next_check_at and (cfg["enabled"] or r.operation_id)]
        hour = now - timedelta(hours=1)
        requests = int((await db.execute(select(func.coalesce(func.sum(QuotaSnapshot.request_count), 0)).where(
            QuotaSnapshot.queried_at >= hour, QuotaSnapshot.error_source == "official_quota"))).scalar_one())
        failures = (await db.execute(select(QuotaSnapshot.error_code, func.count()).where(
            QuotaSnapshot.queried_at >= hour, QuotaSnapshot.success.is_(False)).group_by(QuotaSnapshot.error_code))).all()
        return {**cfg, "queued_count": sum(bool(r.operation_id) for r in rows),
                "max_overdue_seconds": int(max(overdue, default=0)),
                "uncovered_contexts": len({context_key(a.id, w.id if w else None) for a, w in await self.active_contexts(db)} - {r.context_key for r in rows if r.last_attempt_at}),
                "requests_last_hour": requests, "failures": {str(code): n for code, n in failures}}


quota_service = QuotaService()
