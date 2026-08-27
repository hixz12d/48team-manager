"""iCloud HME 客户端：列别名、领下一个未占用、打本地标签。"""
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
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HmeAliasLease, Team
from app.services.child_accounts import normalize_email
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

HME_SETTING_BASE_URL = "hme_base_url"
HME_SETTING_SERVICE_TOKEN = "hme_service_token"
HME_SETTING_ACCOUNT_ID = "hme_account_id"
HME_SETTING_TEAM_TAG_MAP = "hme_team_tag_map"
DEFAULT_HME_BASE_URL = "http://icloud-hme:8081"
FREE_ACCOUNT_LABEL = "GPT已使用"
LEASE_TTL = timedelta(minutes=25)
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


async def active_leased_emails(session: AsyncSession, *, now: Optional[datetime] = None) -> Set[str]:
    current = now or get_now()
    result = await session.execute(
        select(HmeAliasLease.email).where(HmeAliasLease.expires_at > current)
    )
    return {normalize_email(row[0]) for row in result.all() if row[0]}


async def purge_expired_leases(session: AsyncSession, *, now: Optional[datetime] = None) -> None:
    current = now or get_now()
    await session.execute(delete(HmeAliasLease).where(HmeAliasLease.expires_at <= current))


async def release_lease(session: AsyncSession, claimed: Optional[ClaimedAlias]) -> None:
    if not claimed:
        return
    await session.execute(delete(HmeAliasLease).where(HmeAliasLease.id == claimed.lease_id))
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
            purpose=purpose,
            team_id=team_id,
            expires_at=now + LEASE_TTL,
            created_at=now,
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


async def finalize_claim(
    session: AsyncSession,
    claimed: Optional[ClaimedAlias],
    result: Dict[str, Any],
    label: str = "",
) -> None:
    if not claimed:
        return
    if result.get("success") and str(label or "").strip():
        try:
            await apply_local_label(session, claimed, str(label).strip())
        except Exception:
            logger.exception("HME 打标失败 email=%s label=%s，保留租约", claimed.email, label)
            return
        await release_lease(session, claimed)
        return
    await release_lease(session, claimed)


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
