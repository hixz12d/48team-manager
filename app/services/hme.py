"""iCloud HME 客户端：列别名、领下一个未占用、打本地标签。Phase 5 补本地状态机。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChildAccount, HmeAliasLease, Operation, Team
from app.services.child_accounts import CHILD_STATUS_UNUSED, normalize_email
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

HME_SETTING_BASE_URL = "hme_base_url"
HME_SETTING_SERVICE_TOKEN = "hme_service_token"
HME_SETTING_ACCOUNT_ID = "hme_account_id"
HME_SETTING_TEAM_TAG_MAP = "hme_team_tag_map"
DEFAULT_HME_BASE_URL = "http://icloud-hme:8081"
FREE_ACCOUNT_LABEL = "GPT已使用"
LEASE_TTL = timedelta(minutes=25)
HME_STATE_RESERVED = "reserved"
HME_STATE_SIGNUP_STARTED = "signup_started"
HME_STATE_CONSUMED = "consumed"
HME_STATE_QUARANTINED = "quarantined"
HME_STATE_MANUAL_REVIEW = "manual_review"
HME_HELD_STATES = (
    HME_STATE_SIGNUP_STARTED,
    HME_STATE_CONSUMED,
    HME_STATE_QUARANTINED,
    HME_STATE_MANUAL_REVIEW,
)
SIGNUP_STARTED_STAGES = {
    "email_otp",
    "about_you",
    "add_phone",
    "oauth",
}
SIGNUP_STARTED_ERROR_CODES = {
    "mail_otp_timeout",
    "mail_otp_rejected",
    "about_you_stuck",
    "sms_failed",
    "sms_rejected",
    "sms_missing",
    "oauth_callback_missing",
    "oauth_expired",
}
PRE_SIGNUP_ERROR_CODES = {
    "browser_failed",
    "proxy_failed",
    "proxy_missing",
    "mail_missing",
    "cancelled",
    "hme_error",
    "hme_unconfigured",
    "hme_empty",
    "hme_busy",
    "hme_no_account",
    "hme_account_ambiguous",
    "already_on_team",
    "oauth_url_missing",
    "email_input_missing",
    "email_gate_stuck",
}
SERVICE_TOKEN_HEADER = "X-HME-Service-Token"

_SERIAL_DIGIT_RE = re.compile(r"^\d+$")
_SERIAL_ALIAS_RE = re.compile(r"^别名\s*\d+$", re.I)


class HmeError(RuntimeError):
    def __init__(self, message: str, code: str = "hme_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HmeConfig:
    base_url: str = ""
    service_token: str = ""
    account_id: str = ""
    team_tag_map: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.team_tag_map is None:
            self.team_tag_map = {}

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


def is_serial_label(label: str) -> bool:
    """纯数字或「别名 12」只是序号，不当成业务标签。与 HME 前端 isSerialLabel 一致。"""
    tag = str(label or "").strip()
    return bool(_SERIAL_DIGIT_RE.fullmatch(tag) or _SERIAL_ALIAS_RE.fullmatch(tag))


def is_unoccupied_label(label: str) -> bool:
    tag = str(label or "").strip()
    return (not tag) or is_serial_label(tag)


def should_occupy_failed_claim(result: Optional[Dict[str, Any]] = None) -> bool:
    payload = result if isinstance(result, dict) else {}
    if payload.get("success"):
        return False
    if payload.get("occupy_alias") is True:
        return True
    if payload.get("occupy_alias") is False:
        return False
    code = str(payload.get("error_code") or "").strip().lower()
    if code in PRE_SIGNUP_ERROR_CODES:
        return False
    stage = str(payload.get("stage") or payload.get("status") or "").strip().lower()
    if stage in {"queued", "checking", "hme", "browser_open", "mail_missing"}:
        return False
    return bool(code or stage)


def normalize_hme_base_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("HME 地址不能为空")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HME 地址格式无效")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HME 地址只支持 http 或 https")
    if parsed.query or parsed.fragment:
        raise ValueError("HME 地址不能包含查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("HME 地址不能包含路径")
    return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")


def parse_created_at(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def locale_key_zh_cn(text: str) -> Tuple[str, str]:
    """近似 JS localeCompare('zh-CN')：先忽略大小写，再比原文。别名基本是 ASCII。"""
    value = str(text or "")
    return (value.casefold(), value)


def parse_team_tag_map(raw: str) -> Dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Team 标签映射必须是 JSON 对象，例如 {\"3\":\"某某Team\"}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Team 标签映射必须是 JSON 对象")
    out: Dict[str, str] = {}
    for key, value in parsed.items():
        tag = str(value or "").strip()
        if tag:
            out[str(key).strip()] = tag
    return out


def resolve_team_tag(team: Team, mapping: Optional[Dict[str, str]] = None) -> str:
    name = str(team.team_name or "").strip()
    if name:
        return name
    mapping = mapping or {}
    mapped = str(mapping.get(str(team.id)) or "").strip()
    if mapped:
        return mapped
    email = str(team.email or "").strip()
    if "@" in email:
        return email.split("@", 1)[0]
    return email or "team"


def pick_next_unoccupied(
    aliases: Sequence[Dict[str, Any]],
    leased_emails: Iterable[str],
) -> Optional[Dict[str, Any]]:
    leased = {normalize_email(item) for item in leased_emails if item}
    candidates: List[Dict[str, Any]] = []
    for item in aliases:
        if not item.get("active"):
            continue
        email = normalize_email(item.get("email") or "")
        if not email or email in leased:
            continue
        if not is_unoccupied_label(item.get("label") or ""):
            continue
        anonymous_id = str(item.get("anonymousId") or item.get("anonymous_id") or "").strip()
        if not anonymous_id:
            continue
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            parse_created_at(str(item.get("createdAt") or item.get("created_at") or "")),
            locale_key_zh_cn(str(item.get("email") or "")),
        )
    )
    return candidates[0] if candidates else None


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


class HmeClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _request(
        self,
        method: str,
        cfg: HmeConfig,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        origin = normalize_hme_base_url(cfg.base_url)
        token = str(cfg.service_token or "").strip()
        if not token:
            raise HmeError("未配置 HME 服务 token", "hme_unconfigured")
        url = f"{origin}{path}"
        headers = {
            "Accept": "application/json",
            SERVICE_TOKEN_HEADER: token,
        }
        with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, url, headers=headers, json=json_body, params=params)
        if response.status_code == 401:
            raise HmeError("HME 服务 token 无效", "hme_auth")
        if response.status_code < 200 or response.status_code >= 300:
            message = f"HME 返回 HTTP {response.status_code}"
            try:
                payload = response.json()
                message = str(payload.get("message") or message)
            except Exception:  # noqa: BLE001
                pass
            raise HmeError(message, "hme_http")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise HmeError("HME 响应格式无效", "hme_http") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise HmeError(str(payload.get("message") or "HME 调用失败"), str(payload.get("code") or "hme_http"))
        return _unwrap_data(payload)

    def list_accounts(self, cfg: HmeConfig) -> List[Dict[str, Any]]:
        data = self._request("GET", cfg, "/api/accounts")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def list_aliases(self, cfg: HmeConfig, account_id: str) -> List[Dict[str, Any]]:
        data = self._request("GET", cfg, "/api/aliases", params={"account_id": account_id})
        if isinstance(data, dict):
            aliases = data.get("aliases") or []
        else:
            aliases = data or []
        return [item for item in aliases if isinstance(item, dict)]

    def set_local_label(self, cfg: HmeConfig, account_id: str, anonymous_id: str, label: str) -> None:
        self._request(
            "POST",
            cfg,
            f"/api/aliases/{anonymous_id}/label",
            json_body={"account_id": account_id, "label": label},
        )


hme_client = HmeClient()


async def load_config(session: AsyncSession) -> HmeConfig:
    from app.services.settings import settings_service

    base_url = (await settings_service.get_setting(session, HME_SETTING_BASE_URL, DEFAULT_HME_BASE_URL) or DEFAULT_HME_BASE_URL).strip()
    token = (await settings_service.get_setting(session, HME_SETTING_SERVICE_TOKEN, "") or "").strip()
    account_id = (await settings_service.get_setting(session, HME_SETTING_ACCOUNT_ID, "") or "").strip()
    raw_map = await settings_service.get_setting(session, HME_SETTING_TEAM_TAG_MAP, "") or ""
    try:
        mapping = parse_team_tag_map(raw_map)
    except ValueError:
        mapping = {}
    return HmeConfig(base_url=base_url, service_token=token, account_id=account_id, team_tag_map=mapping)


def resolve_account(accounts: Sequence[Dict[str, Any]], account_id: str) -> Dict[str, Any]:
    if not accounts:
        raise HmeError("HME 没有可用账号", "hme_no_account")
    wanted = str(account_id or "").strip()
    if wanted:
        for item in accounts:
            if str(item.get("id") or "") == wanted:
                return item
        raise HmeError(f"HME 账号不存在: {wanted}", "hme_no_account")
    active = [item for item in accounts if str(item.get("status") or "") == "active"]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise HmeError("有多个 HME 账号，请在系统中心指定账号 ID", "hme_account_ambiguous")
    if len(accounts) == 1:
        return accounts[0]
    raise HmeError("没有 active 的 HME 账号", "hme_no_account")


async def _operation_id(session: AsyncSession, job_id: str) -> Optional[int]:
    if not job_id:
        return None
    row = await session.scalar(select(Operation.id).where(Operation.public_id == job_id))
    return int(row) if row is not None else None


def signup_started_from_progress(*, stage: str = "", error_code: str = "") -> bool:
    code = str(error_code or "").strip().lower()
    if code in SIGNUP_STARTED_ERROR_CODES:
        return True
    return str(stage or "").strip().lower() in SIGNUP_STARTED_STAGES


async def active_leased_emails(session: AsyncSession, *, now: Optional[datetime] = None) -> Set[str]:
    current = now or get_now()
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


async def child_occupied_emails(session: AsyncSession) -> Set[str]:
    rows = (
        await session.execute(
            select(ChildAccount.email, ChildAccount.status, ChildAccount.refresh_token_encrypted)
        )
    ).all()
    occupied: Set[str] = set()
    for email, status, refresh in rows:
        if not email:
            continue
        if str(status or "") == CHILD_STATUS_UNUSED and not refresh:
            continue
        occupied.add(normalize_email(email))
    return occupied


async def purge_expired_leases(session: AsyncSession, *, now: Optional[datetime] = None) -> None:
    current = now or get_now()
    await session.execute(
        delete(HmeAliasLease).where(
            HmeAliasLease.expires_at <= current,
            or_(HmeAliasLease.local_state.is_(None), HmeAliasLease.local_state == HME_STATE_RESERVED),
            or_(HmeAliasLease.label_sync_pending.is_(None), HmeAliasLease.label_sync_pending.is_(False)),
        )
    )


async def release_lease(session: AsyncSession, claimed: Optional[ClaimedAlias]) -> None:
    if not claimed:
        return
    lease = await session.get(HmeAliasLease, claimed.lease_id)
    if lease is None:
        return
    if lease.label_sync_pending:
        return
    state = str(lease.local_state or "") or HME_STATE_RESERVED
    if state in {HME_STATE_SIGNUP_STARTED, HME_STATE_QUARANTINED, HME_STATE_MANUAL_REVIEW}:
        return
    await session.delete(lease)
    await session.commit()


async def heartbeat_lease(session: AsyncSession, job_id: str) -> None:
    if not job_id:
        return
    now = get_now()
    rows = list(
        (await session.execute(select(HmeAliasLease).where(HmeAliasLease.job_id == job_id))).scalars()
    )
    for lease in rows:
        lease.heartbeat_at = now
        lease.updated_at = now
    if rows:
        await session.commit()


async def apply_local_label(session: AsyncSession, claimed: ClaimedAlias, label: str) -> None:
    cfg = await load_config(session)
    tag = str(label or "").strip()
    if not tag:
        raise HmeError("标签不能为空", "hme_label")
    await asyncio.to_thread(
        hme_client.set_local_label,
        cfg,
        claimed.account_id,
        claimed.anonymous_id,
        tag,
    )


async def maybe_claim_alias(
    session: AsyncSession,
    email_line: str,
    *,
    job_id: str = "",
    purpose: str = "onboard",
    team_id: Optional[int] = None,
) -> Tuple[str, Optional[ClaimedAlias]]:
    if str(email_line or "").strip():
        return email_line, None
    claimed = await claim_next_alias(session, job_id=job_id, purpose=purpose, team_id=team_id)
    return claimed.email, claimed


async def claim_next_alias(
    session: AsyncSession,
    *,
    job_id: str = "",
    purpose: str = "onboard",
    team_id: Optional[int] = None,
) -> ClaimedAlias:
    cfg = await load_config(session)
    if not cfg.configured:
        raise HmeError("请填写邮箱，或在系统中心配置 iCloud HME", "hme_unconfigured")
    await purge_expired_leases(session)
    accounts = await asyncio.to_thread(hme_client.list_accounts, cfg)
    account = resolve_account(accounts, cfg.account_id)
    account_id = str(account.get("id") or "")
    aliases = await asyncio.to_thread(hme_client.list_aliases, cfg, account_id)
    leased = await active_leased_emails(session)
    leased |= await child_occupied_emails(session)
    now = get_now()
    for _ in range(8):
        picked = pick_next_unoccupied(aliases, leased)
        if not picked:
            raise HmeError("HME 没有未占用别名", "hme_empty")
        email = normalize_email(picked.get("email") or "")
        anonymous_id = str(picked.get("anonymousId") or picked.get("anonymous_id") or "").strip()
        lease = HmeAliasLease(
            email=email,
            anonymous_id=anonymous_id,
            account_id=account_id,
            job_id=job_id or None,
            operation_id=await _operation_id(session, job_id),
            purpose=purpose,
            team_id=team_id,
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
        logger.info("领取 HME 别名 email=%s purpose=%s job=%s", email, purpose, job_id)
        return ClaimedAlias(
            email=email,
            anonymous_id=anonymous_id,
            account_id=account_id,
            lease_id=int(lease.id),
            job_id=job_id,
        )
    raise HmeError("HME 别名租约冲突，请重试", "hme_busy")


async def mark_signup_started(
    session: AsyncSession,
    claimed: Optional[ClaimedAlias],
    *,
    stage: str = "",
    error_code: str = "",
) -> None:
    if not claimed:
        return
    if not signup_started_from_progress(stage=stage, error_code=error_code):
        return
    lease = await session.get(HmeAliasLease, claimed.lease_id)
    if lease is None:
        return
    now = get_now()
    if str(lease.local_state or "") not in {HME_STATE_CONSUMED, HME_STATE_QUARANTINED, HME_STATE_MANUAL_REVIEW}:
        lease.local_state = HME_STATE_SIGNUP_STARTED
    lease.heartbeat_at = now
    lease.updated_at = now
    await session.commit()


async def mark_consumed(
    session: AsyncSession,
    claimed: Optional[ClaimedAlias],
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
    now = get_now()
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
    claimed: Optional[ClaimedAlias],
    result: Dict[str, Any],
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
        except Exception as exc:
            logger.exception("HME 打标失败 email=%s label=%s，保留 consumed + label_sync_pending", claimed.email, tag)
            await mark_consumed(session, claimed, label=tag, pending=True, error=str(exc))
            return
        await mark_consumed(session, claimed, label=tag, pending=False)
        await release_lease(session, claimed)
        return
    await release_lease(session, claimed)


async def reconcile_aliases(
    session: AsyncSession,
    aliases: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """只读对账。发现 conflict 就报告，不自动改远端、不动 Apple。"""
    from app.models import Operation

    now = get_now()
    await purge_expired_leases(session)
    remote_by_email: Dict[str, Dict[str, Any]] = {}
    if aliases is None:
        cfg = await load_config(session)
        if cfg.configured:
            accounts = await asyncio.to_thread(hme_client.list_accounts, cfg)
            account = resolve_account(accounts, cfg.account_id)
            aliases = await asyncio.to_thread(hme_client.list_aliases, cfg, str(account.get("id") or ""))
        else:
            aliases = []
    for item in aliases or []:
        email = normalize_email(item.get("email") or "")
        if email:
            remote_by_email[email] = item
    leases = list((await session.execute(select(HmeAliasLease))).scalars())
    occupied = await child_occupied_emails(session)
    running_ops = {
        str(row.public_id)
        for row in (await session.execute(select(Operation).where(Operation.state.in_(("queued", "running", "waiting"))))).scalars()
    }
    findings: List[Dict[str, Any]] = []
    for lease in leases:
        email = normalize_email(lease.email)
        remote = remote_by_email.get(email)
        remote_unused = bool(remote and remote.get("active") and is_unoccupied_label(remote.get("label") or ""))
        state = str(lease.local_state or HME_STATE_RESERVED)
        if state == HME_STATE_CONSUMED and remote_unused:
            findings.append({"kind": "remote_unused_local_consumed", "email": email, "lease_id": lease.id})
        if lease.expires_at <= now and state == HME_STATE_SIGNUP_STARTED and lease.job_id in running_ops:
            findings.append({"kind": "expired_lease_running_operation", "email": email, "lease_id": lease.id, "job_id": lease.job_id})
        if lease.label_sync_pending:
            findings.append({"kind": "label_sync_pending", "email": email, "lease_id": lease.id, "label": lease.label_desired or ""})
    for email, item in remote_by_email.items():
        if not item.get("active") or is_unoccupied_label(item.get("label") or ""):
            continue
        if email in occupied:
            continue
        if any(normalize_email(lease.email) == email for lease in leases):
            continue
        findings.append({"kind": "remote_used_local_missing", "email": email, "label": item.get("label") or ""})
    return {"ok": True, "conflicts": len(findings), "findings": findings}


async def probe_status(session: AsyncSession) -> Dict[str, Any]:
    cfg = await load_config(session)
    if not cfg.configured:
        return {"ok": False, "error": "未配置 HME 地址或 token", "unused": 0}
    try:
        accounts = await asyncio.to_thread(hme_client.list_accounts, cfg)
        account = resolve_account(accounts, cfg.account_id)
        aliases = await asyncio.to_thread(hme_client.list_aliases, cfg, str(account.get("id") or ""))
        await purge_expired_leases(session)
        leased = await active_leased_emails(session)
        leased |= await child_occupied_emails(session)
        unused = pick_all_unoccupied(aliases, leased)
        return {
            "ok": True,
            "account_id": account.get("id"),
            "account_name": account.get("name") or "",
            "alias_total": len(aliases),
            "unused": len(unused),
        }
    except HmeError as exc:
        return {"ok": False, "error": str(exc), "unused": 0, "error_code": exc.code}
    except Exception as exc:  # noqa: BLE001
        logger.exception("探测 HME 失败")
        return {"ok": False, "error": str(exc) or type(exc).__name__, "unused": 0}


def pick_all_unoccupied(
    aliases: Sequence[Dict[str, Any]],
    leased_emails: Iterable[str],
) -> List[Dict[str, Any]]:
    leased = {normalize_email(item) for item in leased_emails if item}
    out: List[Dict[str, Any]] = []
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
        out.append(item)
    return out
