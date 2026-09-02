"""HME alias leases. Local labels only; never write iCloud."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.settings import get_setting_value
from app.core.time import utcnow
from app.domain.identity.ids import normalize_email
from app.domain.resources import (
    FREE_ACCOUNT_LABEL,
    HME_HELD_STATES,
    HME_STATE_CONSUMED,
    HME_STATE_MANUAL_REVIEW,
    HME_STATE_QUARANTINED,
    HME_STATE_RESERVED,
    HME_STATE_SIGNUP_STARTED,
    LEASE_TTL,
    is_unoccupied_label,
    locale_key_zh_cn,
    normalize_hme_base_url,
    parse_created_at,
    should_occupy_failed_claim,
    signup_started_from_progress,
)
from app.persistence.models.identity import Account
from app.persistence.models.operations import Operation
from app.persistence.models.resources import HmeAliasLease

logger = logging.getLogger(__name__)

DEFAULT_HME_BASE_URL = "http://icloud-hme:8081"
SERVICE_TOKEN_HEADER = "X-HME-Service-Token"


class HmeError(RuntimeError):
    def __init__(self, message: str, code: str = "hme_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HmeConfig:
    base_url: str = ""
    service_token: str = ""
    account_id: str = ""
    team_tag_map: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.service_token)


@dataclass
class ClaimedAlias:
    email: str
    anonymous_id: str
    account_id: str
    lease_id: int
    job_id: str = ""


def pick_next_unoccupied(aliases: list[dict[str, Any]], leased_emails) -> dict[str, Any] | None:
    leased = {normalize_email(item) for item in leased_emails if item}
    candidates: list[dict[str, Any]] = []
    for item in aliases:
        if not item.get("active"):
            continue
        email = normalize_email(item.get("email") or "")
        if not email or email in leased:
            continue
        if not is_unoccupied_label(item.get("label") or ""):
            continue
        if not str(item.get("anonymousId") or item.get("anonymous_id") or "").strip():
            continue
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            parse_created_at(str(item.get("createdAt") or item.get("created_at") or "")),
            locale_key_zh_cn(str(item.get("email") or "")),
        )
    )
    return candidates[0] if candidates else None


class HmeClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _request(self, method: str, cfg: HmeConfig, path: str, **kwargs) -> Any:
        import httpx

        origin = normalize_hme_base_url(cfg.base_url)
        token = str(cfg.service_token or "").strip()
        if not token:
            raise HmeError("HME service token missing", "hme_unconfigured")
        with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            response = client.request(
                method,
                f"{origin}{path}",
                headers={"Accept": "application/json", SERVICE_TOKEN_HEADER: token},
                **kwargs,
            )
        if response.status_code == 401:
            raise HmeError("HME service token invalid", "hme_auth")
        if response.status_code < 200 or response.status_code >= 300:
            raise HmeError(f"HME HTTP {response.status_code}", "hme_http")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise HmeError(str(payload.get("message") or "HME failed"), str(payload.get("code") or "hme_http"))
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def list_accounts(self, cfg: HmeConfig) -> list[dict[str, Any]]:
        data = self._request("GET", cfg, "/api/accounts")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def list_aliases(self, cfg: HmeConfig, account_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", cfg, "/api/aliases", params={"account_id": account_id})
        aliases = data.get("aliases") if isinstance(data, dict) else data
        return [item for item in (aliases or []) if isinstance(item, dict)]

    def set_local_label(self, cfg: HmeConfig, account_id: str, anonymous_id: str, label: str) -> None:
        self._request(
            "POST",
            cfg,
            f"/api/aliases/{anonymous_id}/label",
            json={"account_id": account_id, "label": label},
        )


hme_client = HmeClient()


async def load_config(session: AsyncSession) -> HmeConfig:
    base_url = (await get_setting_value(session, "hme_base_url", DEFAULT_HME_BASE_URL) or DEFAULT_HME_BASE_URL).strip()
    token = (await get_setting_value(session, "hme_service_token", "") or "").strip()
    account_id = (await get_setting_value(session, "hme_account_id", "") or "").strip()
    return HmeConfig(base_url=base_url, service_token=token, account_id=account_id)


def resolve_account(accounts: list[dict[str, Any]], account_id: str) -> dict[str, Any]:
    if not accounts:
        raise HmeError("HME has no accounts", "hme_no_account")
    wanted = str(account_id or "").strip()
    if wanted:
        for item in accounts:
            if str(item.get("id") or "") == wanted:
                return item
        raise HmeError(f"HME account missing: {wanted}", "hme_no_account")
    active = [item for item in accounts if str(item.get("status") or "") == "active"]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise HmeError("multiple HME accounts; set hme_account_id", "hme_account_ambiguous")
    if len(accounts) == 1:
        return accounts[0]
    raise HmeError("no active HME account", "hme_no_account")


async def _operation_id(session: AsyncSession, job_id: str) -> int | None:
    if not job_id:
        return None
    row = await session.scalar(select(Operation.id).where(Operation.public_id == job_id))
    return int(row) if row is not None else None


async def active_leased_emails(session: AsyncSession, *, now=None) -> set[str]:
    current = now or utcnow()
    result = await session.execute(
        select(HmeAliasLease.email).where(
            or_(
                HmeAliasLease.local_state.in_(HME_HELD_STATES),
                HmeAliasLease.label_sync_pending.is_(True),
                HmeAliasLease.expires_at > current,
            )
        )
    )
    return {normalize_email(row[0]) for row in result.all() if row[0]}


async def occupied_account_emails(session: AsyncSession) -> set[str]:
    rows = (await session.execute(select(Account.email, Account.operational_state, Account.local_purpose))).all()
    occupied: set[str] = set()
    for email, state, purpose in rows:
        if not email:
            continue
        if str(state or "") in {"unused", "archived", "disabled"} or str(purpose or "") in {"disabled", "free"}:
            continue
        occupied.add(normalize_email(email))
    return occupied


async def purge_expired_leases(session: AsyncSession, *, now=None) -> None:
    current = now or utcnow()
    await session.execute(
        delete(HmeAliasLease).where(
            HmeAliasLease.expires_at <= current,
            or_(HmeAliasLease.local_state.is_(None), HmeAliasLease.local_state == HME_STATE_RESERVED),
            or_(HmeAliasLease.label_sync_pending.is_(None), HmeAliasLease.label_sync_pending.is_(False)),
        )
    )


async def release_lease(session: AsyncSession, claimed: ClaimedAlias | None) -> None:
    if not claimed:
        return
    lease = await session.get(HmeAliasLease, claimed.lease_id)
    if lease is None or lease.label_sync_pending:
        return
    state = str(lease.local_state or "") or HME_STATE_RESERVED
    if state in {HME_STATE_SIGNUP_STARTED, HME_STATE_QUARANTINED, HME_STATE_MANUAL_REVIEW}:
        return
    await session.delete(lease)
    await session.commit()


async def apply_local_label(session: AsyncSession, claimed: ClaimedAlias, label: str) -> None:
    cfg = await load_config(session)
    tag = str(label or "").strip()
    if not tag:
        raise HmeError("label is empty", "hme_label")
    hme_client.set_local_label(cfg, claimed.account_id, claimed.anonymous_id, tag)


def resolve_workspace_tag(workspace, mapping: dict[str, str] | None = None) -> str:
    name = str(getattr(workspace, "name", "") or "").strip()
    if name:
        return name
    mapped = str((mapping or {}).get(str(getattr(workspace, "id", "") or "")) or "").strip()
    if mapped:
        return mapped
    owner = getattr(workspace, "owner_account", None)
    email = str(getattr(owner, "email", "") or "").strip()
    if "@" in email:
        return email.split("@", 1)[0]
    return email or "team"


async def claim_next_alias(
    session: AsyncSession,
    *,
    job_id: str = "",
    purpose: str = "onboard",
    workspace_id: int | None = None,
) -> ClaimedAlias:
    cfg = await load_config(session)
    if not cfg.configured:
        raise HmeError("HME is not configured", "hme_unconfigured")
    await purge_expired_leases(session)
    accounts = hme_client.list_accounts(cfg)
    account = resolve_account(accounts, cfg.account_id)
    account_id = str(account.get("id") or "")
    aliases = hme_client.list_aliases(cfg, account_id)
    leased = await active_leased_emails(session)
    leased |= await occupied_account_emails(session)
    now = utcnow()
    for _ in range(8):
        picked = pick_next_unoccupied(aliases, leased)
        if not picked:
            raise HmeError("HME has no unused alias", "hme_empty")
        email = normalize_email(picked.get("email") or "")
        anonymous_id = str(picked.get("anonymousId") or picked.get("anonymous_id") or "").strip()
        lease = HmeAliasLease(
            email=email,
            anonymous_id=anonymous_id,
            account_id=account_id,
            job_id=job_id or None,
            operation_id=await _operation_id(session, job_id),
            purpose=purpose,
            workspace_id=workspace_id,
            local_state=HME_STATE_RESERVED,
            label_sync_pending=False,
            heartbeat_at=now,
            expires_at=now + LEASE_TTL,
            created_at=now,
            updated_at=now,
        )
        session.add(lease)
        try:
            await session.flush()
            await session.commit()
        except IntegrityError:
            await session.rollback()
            leased.add(email)
            continue
        return ClaimedAlias(
            email=email,
            anonymous_id=anonymous_id,
            account_id=account_id,
            lease_id=int(lease.id),
            job_id=job_id,
        )
    raise HmeError("HME lease conflict", "hme_busy")


async def maybe_claim_alias(
    session: AsyncSession,
    email_line: str,
    *,
    job_id: str = "",
    purpose: str = "onboard",
    workspace_id: int | None = None,
) -> tuple[str, ClaimedAlias | None]:
    if str(email_line or "").strip():
        return email_line, None
    claimed = await claim_next_alias(session, job_id=job_id, purpose=purpose, workspace_id=workspace_id)
    return claimed.email, claimed


async def mark_signup_started(
    session: AsyncSession,
    claimed: ClaimedAlias | None,
    *,
    stage: str = "",
    error_code: str = "",
) -> None:
    if not claimed or not signup_started_from_progress(stage=stage, error_code=error_code):
        return
    lease = await session.get(HmeAliasLease, claimed.lease_id)
    if lease is None:
        return
    now = utcnow()
    if str(lease.local_state or "") not in {HME_STATE_CONSUMED, HME_STATE_QUARANTINED, HME_STATE_MANUAL_REVIEW}:
        lease.local_state = HME_STATE_SIGNUP_STARTED
    lease.heartbeat_at = now
    lease.updated_at = now
    await session.commit()


async def mark_consumed(
    session: AsyncSession,
    claimed: ClaimedAlias | None,
    *,
    label: str = "",
    pending: bool = False,
    error: str = "",
) -> None:
    if not claimed:
        return
    lease = await session.get(HmeAliasLease, claimed.lease_id)
    if lease is None:
        return
    now = utcnow()
    lease.local_state = HME_STATE_CONSUMED
    if label:
        lease.label_desired = label
    lease.label_sync_pending = bool(pending)
    if error:
        lease.last_error = str(error)[:500]
    lease.heartbeat_at = now
    lease.updated_at = now
    await session.commit()


async def finalize_claim(
    session: AsyncSession,
    claimed: ClaimedAlias | None,
    result: dict[str, Any],
    label: str = "",
) -> None:
    if not claimed:
        return
    tag = str(label or "").strip()
    occupy = bool(result.get("success") and tag)
    if not occupy and not result.get("success") and should_occupy_failed_claim(result):
        occupy = True
        tag = tag or FREE_ACCOUNT_LABEL
    if occupy:
        await mark_consumed(session, claimed, label=tag, pending=True)
        try:
            await apply_local_label(session, claimed, tag)
        except Exception as exc:  # noqa: BLE001
            logger.exception("HME label failed email=%s", claimed.email)
            await mark_consumed(session, claimed, label=tag, pending=True, error=str(exc))
            return
        await mark_consumed(session, claimed, label=tag, pending=False)
        await release_lease(session, claimed)
        return
    await release_lease(session, claimed)


async def reconcile_aliases(
    session: AsyncSession,
    aliases: list[dict[str, Any]] | None = None,
    readonly: bool = True,
) -> dict[str, Any]:
    now = utcnow()
    if not readonly:
        await purge_expired_leases(session)
    remote_by_email: dict[str, dict[str, Any]] = {}
    if aliases is None:
        cfg = await load_config(session)
        aliases = []
        if cfg.configured:
            accounts = hme_client.list_accounts(cfg)
            account = resolve_account(accounts, cfg.account_id)
            aliases = hme_client.list_aliases(cfg, str(account.get("id") or ""))
    for item in aliases or []:
        email = normalize_email(item.get("email") or "")
        if email:
            remote_by_email[email] = item
    leases = list((await session.execute(select(HmeAliasLease))).scalars())
    occupied = await occupied_account_emails(session)
    running_ops = {
        str(row.public_id)
        for row in (
            await session.execute(select(Operation).where(Operation.state.in_(("queued", "running", "waiting"))))
        ).scalars()
    }
    findings: list[dict[str, Any]] = []
    for lease in leases:
        email = normalize_email(lease.email)
        remote = remote_by_email.get(email)
        remote_unused = bool(remote and remote.get("active") and is_unoccupied_label(remote.get("label") or ""))
        state = str(lease.local_state or HME_STATE_RESERVED)
        if state == HME_STATE_CONSUMED and remote_unused:
            findings.append({"kind": "remote_unused_local_consumed", "email": email, "lease_id": lease.id})
        if lease.expires_at <= now and state == HME_STATE_SIGNUP_STARTED and lease.job_id in running_ops:
            findings.append(
                {
                    "kind": "expired_lease_running_operation",
                    "email": email,
                    "lease_id": lease.id,
                    "job_id": lease.job_id,
                }
            )
        if lease.label_sync_pending:
            findings.append(
                {
                    "kind": "label_sync_pending",
                    "email": email,
                    "lease_id": lease.id,
                    "label": lease.label_desired or "",
                }
            )
    for email, item in remote_by_email.items():
        if not item.get("active") or is_unoccupied_label(item.get("label") or ""):
            continue
        if email in occupied:
            continue
        if any(normalize_email(lease.email) == email for lease in leases):
            continue
        findings.append({"kind": "remote_used_local_missing", "email": email, "label": item.get("label") or ""})
    return {"ok": True, "conflicts": len(findings), "findings": findings}


async def list_leases(session: AsyncSession) -> list[HmeAliasLease]:
    return list((await session.execute(select(HmeAliasLease).order_by(HmeAliasLease.id.asc()))).scalars())
