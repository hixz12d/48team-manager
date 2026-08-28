"""Sub2API Admin 导入与只读状态。同机部署，不走代理。"""
from __future__ import annotations

import logging
import asyncio
import re
import pytz
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Sub2ApiUsageLedger

from app.services.settings import settings_service
from app.config import settings
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

_TEAM_PREFIX_RE = re.compile(r"^(?:GPT\s+)?Team\s*", re.IGNORECASE)
_CHILD_RE = re.compile(r"子号\s*(\d+)")
_FREE_NAME_RE = re.compile(r"^Free\s+(\d+)\s*$", re.IGNORECASE)
_DEFAULT_FREE_TEMPLATE = "Free模板"
_EMAIL_FAMILY_RE = re.compile(r"(?:xiaozhudf\.?)?(\d{4}\.\d+)", re.IGNORECASE)
_XIAOZHU_DOTTED_RE = re.compile(r"xiaozhudf\.(\d{4}\.\d+)", re.IGNORECASE)
_XIAOZHU_PLAIN_RE = re.compile(r"xiaozhudf(\d{4}\.\d+)", re.IGNORECASE)
_NEWXIAOZHU_RE = re.compile(r"^newxiaozhu(.*)$", re.IGNORECASE)
_DEFAULT_CHILD_TEMPLATE = "Team轮转"
_STATUS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STATUS_CACHE_TTL = 45.0
_USAGE_COST_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}
_USAGE_COST_TTL = 90.0


def invalidate_status_cache() -> None:
    _STATUS_CACHE.clear()


class Sub2ApiService:
    async def _config(self, db_session: AsyncSession) -> Dict[str, Any]:
        base_url = (await settings_service.get_setting(db_session, "sub2api_base_url", "")).strip().rstrip("/")
        api_key = (await settings_service.get_setting(db_session, "sub2api_api_key", "")).strip()
        email = (await settings_service.get_setting(db_session, "sub2api_admin_email", "")).strip()
        password = (await settings_service.get_setting(db_session, "sub2api_admin_password", "")).strip()
        group_raw = (await settings_service.get_setting(db_session, "sub2api_group_ids", "")).strip()
        group_ids: List[int] = []
        if group_raw:
            for part in group_raw.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    group_ids.append(int(part))
        template_name = (await settings_service.get_setting(db_session, "sub2api_template_name", _DEFAULT_CHILD_TEMPLATE)).strip()
        free_template_name = (await settings_service.get_setting(db_session, "sub2api_free_template_name", _DEFAULT_FREE_TEMPLATE)).strip()
        return {
            "base_url": base_url or "http://sub2api-canary:8080",
            "api_key": api_key,
            "email": email,
            "password": password,
            "group_ids": group_ids,
            "template_name": template_name or _DEFAULT_CHILD_TEMPLATE,
            "free_template_name": free_template_name or _DEFAULT_FREE_TEMPLATE,
        }

    def _unwrap(self, data: Any) -> Any:
        if isinstance(data, dict) and "code" in data:
            if data.get("code") not in (0, "0", None, 200):
                raise RuntimeError(data.get("message") or str(data))
            return data.get("data", data)
        return data

    def _extract_account_id(self, data: Any) -> Optional[int]:
        if isinstance(data, dict):
            for key in ("id", "account_id"):
                value = data.get(key)
                if value is not None and str(value).isdigit():
                    return int(value)
            items = data.get("items")
            if isinstance(items, list):
                for item in items:
                    found = self._extract_account_id(item)
                    if found:
                        return found
            nested = data.get("data")
            if nested is not data:
                return self._extract_account_id(nested)
        if isinstance(data, list):
            for item in data:
                found = self._extract_account_id(item)
                if found:
                    return found
        return None

    def _account_items(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("items", "accounts", "rows", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        nested = data.get("data")
        if nested is not data:
            return self._account_items(nested)
        return []

    def _account_email(self, account: Dict[str, Any]) -> str:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        cred = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        return str(cred.get("email") or extra.get("email") or "").strip()

    def _parse_when(self, value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return self._parse_when(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _is_future(self, value: Any) -> bool:
        when = self._parse_when(value)
        if when is None:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when > datetime.now(timezone.utc)

    def _format_remain(self, value: Any) -> str:
        when = self._parse_when(value)
        if when is None:
            return ""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            return "已过"
        hours, rem = divmod(seconds, 3600)
        minutes = rem // 60
        if hours >= 48:
            return f"{hours // 24}天"
        if hours:
            return f"{hours}小时"
        return f"{minutes}分"

    def _account_extra(self, account: Dict[str, Any]) -> Dict[str, Any]:
        extra = account.get("extra")
        return extra if isinstance(extra, dict) else {}

    def _parse_percent(self, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            return None

    def _quota_percent(self, account: Dict[str, Any]) -> Optional[int]:
        return self._parse_percent(self._account_extra(account).get("codex_7d_used_percent"))

    def _weekly_reset_at(self, account: Dict[str, Any]) -> Any:
        extra = self._account_extra(account)
        return extra.get("codex_7d_reset_at") or extra.get("codex_secondary_reset_at")

    def _five_hour_percent(self, account: Dict[str, Any]) -> Optional[int]:
        extra = self._account_extra(account)
        return self._parse_percent(extra.get("codex_5h_used_percent") or extra.get("codex_primary_used_percent"))

    def _five_hour_reset_at(self, account: Dict[str, Any]) -> Any:
        extra = self._account_extra(account)
        return extra.get("codex_5h_reset_at") or extra.get("codex_primary_reset_at")

    def _looks_weekly_reset(self, value: Any) -> bool:
        when = self._parse_when(value)
        if when is None:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (when - datetime.now(timezone.utc)).total_seconds() >= 6 * 3600

    def _schedule_state(self, account: Dict[str, Any]) -> Dict[str, str]:
        error = str(account.get("error_message") or "").strip()
        probe = self.classify_probe(status_code=None, error=error, payload=account)
        if probe["kind"] == "phone":
            return {"kind": "phone", "label": "未接码", "tone": "warn"}
        lowered = error.lower()
        if probe["kind"] == "401" or "401" in error or "revoked" in lowered or "invalidated oauth" in lowered:
            return {"kind": "401", "label": "401 失效", "tone": "danger"}
        if probe["kind"] == "403":
            return {"kind": "403", "label": "403", "tone": "danger"}
        weekly_pct = self._quota_percent(account)
        weekly_reset = self._weekly_reset_at(account)
        if weekly_pct is not None and weekly_pct >= 100:
            remain = self._format_remain(weekly_reset)
            if not remain:
                generic = account.get("rate_limit_reset_at")
                if self._looks_weekly_reset(generic):
                    remain = self._format_remain(generic)
            label = f"429 {remain}" if remain else "429 限额"
            return {"kind": "429", "label": label, "tone": "warn"}
        five_pct = self._five_hour_percent(account)
        five_reset = self._five_hour_reset_at(account)
        if five_pct is not None and five_pct >= 100 and (not five_reset or self._is_future(five_reset)):
            remain = self._format_remain(five_reset)
            if not remain:
                generic = account.get("rate_limit_reset_at")
                if generic and not self._looks_weekly_reset(generic):
                    remain = self._format_remain(generic)
            label = f"5h限制 {remain}" if remain else "5h限制"
            return {"kind": "5h", "label": label, "tone": "warn"}
        if account.get("status") == "error":
            return {"kind": "error", "label": "异常", "tone": "danger"}
        if account.get("schedulable") is False:
            return {"kind": "paused", "label": "不调度", "tone": "muted"}
        return {"kind": "ok", "label": "可调度", "tone": "ok"}

    def _family_from_name(self, name: str) -> Tuple[str, str, int]:
        text = (name or "").strip()
        if not text:
            return "", "account", 0
        child = _CHILD_RE.search(text)
        role = "child" if child else ("owner" if "母号" in text else "account")
        order = int(child.group(1)) if child else (0 if role == "owner" else 99)
        family = _TEAM_PREFIX_RE.sub("", text)
        family = _CHILD_RE.sub("", family)
        family = family.replace("母号", "").strip(" -_/")
        family = re.sub(r"\s+", " ", family).strip()
        return self._normalize_family(family), role, order

    def _normalize_family(self, family: str) -> str:
        text = re.sub(r"\s+", " ", (family or "").strip())
        match = _EMAIL_FAMILY_RE.search(text)
        if match:
            return f".{match.group(1)}"
        if "pedro" in text.lower():
            return "Pedro"
        return text

    def _family_from_email(self, email: str) -> str:
        match = _EMAIL_FAMILY_RE.search(email or "")
        if match:
            return f".{match.group(1)}"
        local = (email or "").split("@", 1)[0].strip().lower()
        if "pedro" in local:
            return "Pedro"
        return ""

    def _positive_ids(self, values: Any) -> List[int]:
        if not isinstance(values, list):
            return []
        out: List[int] = []
        seen = set()
        for item in values:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number <= 0 or number in seen:
                continue
            seen.add(number)
            out.append(number)
        return out

    def _account_group_ids(self, account: Optional[Dict[str, Any]]) -> List[int]:
        if not account:
            return []
        groups = account.get("groups")
        if not groups:
            groups = account.get("group_ids")
        ids: List[int] = []
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict):
                    ids.append(group.get("id"))
                else:
                    ids.append(group)
        return self._positive_ids(ids)

    def _account_proxy_id(self, account: Optional[Dict[str, Any]]) -> Optional[int]:
        if not account:
            return None
        value = account.get("proxy_id")
        if value in (None, "", 0, "0"):
            proxy = account.get("proxy")
            if isinstance(proxy, dict):
                value = proxy.get("id")
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def proxy_endpoint(self, host: Any, port: Any = None) -> str:
        text = str(host or "").strip().lower().strip("[]")
        if not text:
            return ""
        try:
            number = int(port) if port not in (None, "") else 0
        except (TypeError, ValueError):
            number = 0
        return f"{text}:{number}" if number else text

    def proxy_endpoint_from_url(self, proxy_url: str) -> str:
        raw = str(proxy_url or "").strip()
        if not raw:
            return ""
        try:
            parsed = urlparse(raw if "://" in raw else f"socks5h://{raw}")
        except Exception:
            return ""
        return self.proxy_endpoint(parsed.hostname, parsed.port)

    def proxy_endpoint_from_item(self, item: Dict[str, Any]) -> str:
        return self.proxy_endpoint(item.get("host"), item.get("port"))

    def match_proxy_id(
        self,
        proxies: List[Dict[str, Any]],
        proxy_url: str,
        siblings: Optional[List[Dict[str, Any]]] = None,
        family_label: str = "",
    ) -> Optional[int]:
        target = self.proxy_endpoint_from_url(proxy_url)
        if target:
            for item in proxies:
                if self.proxy_endpoint_from_item(item) != target:
                    continue
                try:
                    number = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
        if family_label:
            for item in proxies:
                proxy_family = self._family_from_name(str(item.get("name") or ""))[0]
                if not self.families_match(family_label, proxy_family):
                    continue
                try:
                    number = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
        counts: Dict[int, int] = {}
        for account in siblings or []:
            proxy_id = self._account_proxy_id(account)
            if proxy_id:
                counts[proxy_id] = counts.get(proxy_id, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda item: (item[1], item[0]))[0]

    def classify_probe(
        self,
        *,
        status_code: Optional[int] = None,
        error: str = "",
        payload: Any = None,
    ) -> Dict[str, str]:
        parts = [str(error or "")]
        if isinstance(payload, dict):
            for key in ("error", "error_message", "message", "detail", "error_code"):
                value = payload.get(key)
                if value not in (None, ""):
                    parts.append(str(value))
            nested = payload.get("error")
            if isinstance(nested, dict):
                parts.extend(str(nested.get(key) or "") for key in ("message", "code", "type"))
        blob = " ".join(parts).lower()
        phone_markers = (
            "phone number",
            "phone verification",
            "verify your phone",
            "add a phone",
            "sms_verification",
            "phone_verification",
            "手机验证",
            "未接码",
        )
        if any(marker in blob for marker in phone_markers):
            return {"kind": "phone", "label": "未接码", "tone": "warn"}
        code = 0
        try:
            code = int(status_code or 0)
        except (TypeError, ValueError):
            code = 0
        if code == 401 or "401" in blob or "revoked" in blob or "invalidated oauth" in blob or "token_invalidated" in blob:
            return {"kind": "401", "label": "401", "tone": "danger"}
        if code == 403 or "403" in blob:
            return {"kind": "403", "label": "403", "tone": "danger"}
        if code == 429 or "429" in blob or "rate limit" in blob:
            return {"kind": "429", "label": "429", "tone": "warn"}
        if code >= 400:
            return {"kind": str(code), "label": str(code), "tone": "danger"}
        if isinstance(payload, dict) and payload.get("ok") is False:
            return {"kind": "error", "label": "异常", "tone": "danger"}
        if code == 0 and not blob.strip():
            return {"kind": "none", "label": "无token", "tone": "muted"}
        return {"kind": "200", "label": "200", "tone": "ok"}

    def extra_from_template_values(self, values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if not isinstance(values, dict):
            return extra
        ws = str(values.get("openai_ws_mode") or "off").strip() or "off"
        extra["openai_oauth_responses_websockets_v2_mode"] = ws
        extra["openai_oauth_responses_websockets_v2_enabled"] = ws not in {"", "off"}
        fingerprint = str(values.get("codex_fingerprint_mode") or "off").strip() or "off"
        if fingerprint != "off":
            extra["codex_fingerprint_mode"] = fingerprint
        if values.get("tls_fingerprint_enabled"):
            extra["enable_tls_fingerprint"] = True
            profile = values.get("tls_fingerprint_profile_id")
            if profile not in (None, 0, "0"):
                extra["tls_fingerprint_profile_id"] = profile
        if values.get("openai_passthrough"):
            extra["openai_passthrough"] = True
        if values.get("openai_flatten_namespaces"):
            extra["openai_flatten_namespaces"] = True
        if values.get("openai_long_context_billing"):
            extra["openai_long_context_billing_enabled"] = True
        compact = str(values.get("openai_compact_mode") or "auto").strip() or "auto"
        if compact != "auto":
            extra["openai_compact_mode"] = compact
        if values.get("codex_cli_only"):
            extra["codex_cli_only"] = True
        if values.get("codex_cli_only_app_server"):
            extra["codex_cli_only_allow_app_server"] = True
        return extra

    def pick_account_create_template(
        self,
        items: List[Dict[str, Any]],
        *,
        name: str = "",
        platform: str = "openai",
        account_type: str = "oauth",
    ) -> Optional[Dict[str, Any]]:
        scoped = [
            item for item in items
            if str(item.get("platform") or "") == platform and str(item.get("type") or "") == account_type
        ]
        want = (name or "").strip().lower()
        if want:
            for item in scoped:
                if str(item.get("name") or "").strip().lower() == want:
                    return item
        for item in scoped:
            if item.get("is_default"):
                return item
        for item in scoped:
            if str(item.get("name") or "").strip() == _DEFAULT_CHILD_TEMPLATE:
                return item
        return scoped[0] if scoped else None

    def template_import_fields(
        self,
        template: Optional[Dict[str, Any]],
        fallback_group_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(template, dict):
            return {
                "name": "",
                "group_ids": list(fallback_group_ids or []),
                "concurrency": None,
                "priority": None,
                "rate_multiplier": None,
                "load_factor": None,
                "auto_pause_on_expired": None,
                "extra": None,
            }
        values = (template or {}).get("values") if isinstance(template, dict) else None
        if not isinstance(values, dict):
            values = {}
        include_groups = bool((template or {}).get("include_groups"))
        group_ids = self._positive_ids(values.get("group_ids")) if include_groups else []
        if not group_ids:
            group_ids = list(fallback_group_ids or [])
        concurrency = values.get("concurrency")
        try:
            concurrency = int(concurrency) if concurrency not in (None, "") else None
        except (TypeError, ValueError):
            concurrency = None
        priority = values.get("priority")
        try:
            priority = int(priority) if priority not in (None, "") else None
        except (TypeError, ValueError):
            priority = None
        extra = self.extra_from_template_values(values)
        return {
            "name": str((template or {}).get("name") or "").strip(),
            "group_ids": group_ids,
            "concurrency": concurrency if concurrency and concurrency > 0 else None,
            "priority": priority if priority and priority > 0 else None,
            "rate_multiplier": values.get("rate_multiplier"),
            "load_factor": values.get("load_factor"),
            "auto_pause_on_expired": values.get("auto_pause_on_expired"),
            "extra": extra or None,
        }

    def derive_family_label(self, email: str, team_name: str = "") -> str:
        text = email or ""
        dotted = _XIAOZHU_DOTTED_RE.search(text)
        if dotted:
            return f".{dotted.group(1)}"
        plain = _XIAOZHU_PLAIN_RE.search(text)
        if plain:
            return plain.group(1)
        local = text.split("@", 1)[0].strip()
        if "pedro" in local.lower():
            return "Pedro"
        newbie = _NEWXIAOZHU_RE.match(local)
        if newbie:
            suffix = (newbie.group(1) or "").strip()
            return f"new{suffix}" if suffix else "new"
        email_family = self._family_from_email(text)
        if email_family:
            return email_family
        if team_name:
            return self._normalize_family(team_name)
        return local

    def family_key(self, email: str, team_name: str = "", name: str = "") -> str:
        return self._family_from_email(email) or self._family_from_name(name)[0] or self._normalize_family(
            self.derive_family_label(email, team_name)
        )

    def _label_from_account_name(self, name: str) -> str:
        text = str(name or "").strip()
        if not text or "@" in text:
            return ""
        text = _TEAM_PREFIX_RE.sub("", text)
        text = re.sub(r"^母号\s*", "", text)
        text = re.sub(r"\s*母号\s*$", "", text)
        text = _CHILD_RE.sub("", text)
        return text.strip(" -_/")

    def family_accounts(
        self,
        accounts: List[Dict[str, Any]],
        team_email: str,
        team_name: str = "",
    ) -> List[Dict[str, Any]]:
        key = self.family_key(team_email, team_name)
        if not key:
            return []
        matched = []
        for account in accounts:
            account_key = self.family_key(self._account_email(account), name=str(account.get("name") or ""))
            if account_key == key:
                matched.append(account)
        return matched

    def display_family_label(
        self,
        accounts: List[Dict[str, Any]],
        team_email: str,
        team_name: str = "",
    ) -> str:
        derived = self.derive_family_label(team_email, team_name)
        labels = []
        for account in self.family_accounts(accounts, team_email, team_name):
            label = self._label_from_account_name(str(account.get("name") or ""))
            if label:
                labels.append(label)
        if labels:
            return max(set(labels), key=labels.count)
        return derived

    def next_child_order(self, accounts: List[Dict[str, Any]]) -> int:
        used = []
        for account in accounts:
            match = _CHILD_RE.search(str(account.get("name") or ""))
            if match:
                used.append(int(match.group(1)))
        return (max(used) + 1) if used else 1

    def next_free_order(self, accounts: List[Dict[str, Any]]) -> int:
        used = []
        for account in accounts:
            match = _FREE_NAME_RE.search(str(account.get("name") or "").strip())
            if match:
                used.append(int(match.group(1)))
        return (max(used) + 1) if used else 1

    def build_free_account_name(
        self,
        *,
        email: str,
        accounts: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        accounts = accounts or []
        target = (email or "").strip().lower()
        for account in accounts:
            name = str(account.get("name") or "").strip()
            if name and self._account_email(account).strip().lower() == target:
                return name
        return f"Free {self.next_free_order(accounts)}"

    def find_existing_account(
        self,
        accounts: List[Dict[str, Any]],
        *,
        email: str = "",
        existing_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if existing_id:
            for account in accounts:
                if account.get("id") == existing_id:
                    return account
        target = (email or "").strip().lower()
        if not target:
            return None
        for account in accounts:
            if self._account_email(account).strip().lower() == target:
                return account
        return None

    def build_account_name(
        self,
        *,
        role: str,
        email: str,
        team_email: str,
        team_name: str = "",
        accounts: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        accounts = accounts or []
        target = (email or "").strip().lower()
        for account in accounts:
            name = str(account.get("name") or "").strip()
            if name and self._account_email(account).strip().lower() == target:
                return name
        label = self.display_family_label(accounts, team_email or email, team_name)
        if not label:
            return email
        if role == "owner":
            return f"Team {label} 母号"
        family = self.family_accounts(accounts, team_email or email, team_name)
        return f"Team {label} 子号 {self.next_child_order(family)}"

    def build_codex_import_payload(
        self,
        *,
        content: str,
        name: str,
        role: str,
        existing: Optional[Dict[str, Any]],
        template_fields: Optional[Dict[str, Any]] = None,
        fallback_group_ids: Optional[List[int]] = None,
        proxy_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "content": content,
            "name": name,
            "update_existing": True,
        }
        fields = template_fields or {}
        fallback = list(fallback_group_ids or [])
        create = existing is None
        group_ids: List[int] = []
        apply_template = create and role == "child"
        if apply_template:
            group_ids = list(fields.get("group_ids") or fallback)
            if fields.get("concurrency"):
                payload["concurrency"] = fields["concurrency"]
            if fields.get("priority"):
                payload["priority"] = fields["priority"]
            if fields.get("rate_multiplier") is not None:
                payload["rate_multiplier"] = fields["rate_multiplier"]
            if fields.get("load_factor"):
                payload["load_factor"] = fields["load_factor"]
            if fields.get("extra"):
                payload["extra"] = dict(fields["extra"])
            if fields.get("auto_pause_on_expired") is not None:
                payload["auto_pause_on_expired"] = bool(fields["auto_pause_on_expired"])
        elif create:
            group_ids = fallback
        elif not self._account_group_ids(existing):
            group_ids = list(fields.get("group_ids") or fallback) if role == "child" else fallback
        if group_ids:
            payload["group_ids"] = group_ids
            payload["confirm_mixed_channel_risk"] = True
        if proxy_id and (create or not self._account_proxy_id(existing)):
            payload["proxy_id"] = proxy_id
        return payload


    def build_oauth_credentials(
        self,
        *,
        email: str,
        access_token: str,
        refresh_token: str = "",
        id_token: str = "",
        account_id: str = "",
        client_id: str = "",
    ) -> Dict[str, Any]:
        credentials: Dict[str, Any] = {"access_token": access_token, "email": email}
        if refresh_token:
            credentials["refresh_token"] = refresh_token
        if id_token:
            credentials["id_token"] = id_token
        if account_id:
            credentials["chatgpt_account_id"] = account_id
        if client_id:
            credentials["client_id"] = client_id
        return credentials

    def summarize_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        name = str(account.get("name") or "").strip()
        email = self._account_email(account)
        family, role, order = self._family_from_name(name)
        email_family = self._family_from_email(email)
        if email_family:
            family = email_family
        elif not family:
            family = name or email or f"#{account.get('id')}"
        if role != "child":
            role = "owner" if email.endswith("@gmail.com") or "母号" in name else role
            order = 0 if role == "owner" else order
        schedule = self._schedule_state(account)
        quota = self._quota_percent(account)
        short_name = name or email or f"#{account.get('id')}"
        if role == "owner":
            short_name = "母号"
        elif role == "child" and order:
            short_name = f"子号 {order}"
        return {
            "id": account.get("id"),
            "name": name,
            "short_name": short_name,
            "email": email,
            "family": family,
            "role": role,
            "order": order,
            "quota": quota,
            "quota_label": f"7日 {quota}%" if quota is not None else "额度 -",
            "schedule": schedule["kind"],
            "schedule_label": schedule["label"],
            "tone": schedule["tone"],
            "account_cost": None,
            "user_cost": None,
            "account_cost_label": "",
            "user_cost_label": "",
            "lifetime_account_cost": None,
            "lifetime_user_cost": None,
            "lifetime_account_cost_label": "",
            "lifetime_user_cost_label": "",
        }

    def _is_relevant_account(self, account: Dict[str, Any]) -> bool:
        name = str(account.get("name") or "")
        email = self._account_email(account)
        blob = f"{name} {email}".lower()
        if "team" in blob or "pedro" in blob:
            return True
        return bool(_EMAIL_FAMILY_RE.search(name) or _EMAIL_FAMILY_RE.search(email))

    def index_status_by_email(self, boxes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        for box in boxes:
            for account in box.get("accounts") or []:
                email = str(account.get("email") or "").strip().lower()
                if email:
                    index[email] = account
        return index

    def group_accounts(self, accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for account in accounts:
            row = self.summarize_account(account)
            grouped.setdefault(row["family"], []).append(row)
        boxes = []
        for family, rows in grouped.items():
            rows.sort(key=lambda item: (0 if item["role"] == "owner" else 1, item["order"], item["name"]))
            tones = {item["tone"] for item in rows}
            if "danger" in tones:
                tone = "danger"
            elif "warn" in tones:
                tone = "warn"
            else:
                tone = "ok"
            boxes.append({
                "title": family,
                "tone": tone,
                "accounts": rows,
            })
        boxes.sort(key=lambda item: item["title"].lower())
        return boxes

    def _local_datetime(self, value: Any) -> Optional[datetime]:
        when = self._parse_when(value)
        if when is None:
            return None
        if when.tzinfo is None:
            return when
        tz = pytz.timezone(settings.timezone)
        return when.astimezone(tz).replace(tzinfo=None)

    def _is_today(self, value: Any, today) -> bool:
        local = self._local_datetime(value)
        return bool(local and local.date() == today)

    def _family_token(self, value: str) -> str:
        return re.sub(r"\s+", "", (value or "").strip()).lower().lstrip(".")

    def families_match(self, left: str, right: str) -> bool:
        first = self._family_token(left)
        second = self._family_token(right)
        return bool(first and second and first == second)

    def _card_family(self, card: Dict[str, Any]) -> str:
        email = str(card.get("email") or "")
        team_name = str(card.get("team_name") or "")
        return self.family_key(email, team_name) or self.derive_family_label(email, team_name)

    def _box_matches_card(self, box: Dict[str, Any], card: Dict[str, Any]) -> bool:
        family = self._card_family(card)
        if self.families_match(family, str(box.get("title") or "")):
            return True
        card_email = str(card.get("email") or "").strip().lower()
        if card_email and any(
            str(item.get("email") or "").strip().lower() == card_email
            for item in (box.get("accounts") or [])
        ):
            return True
        for item in box.get("accounts") or []:
            if self.families_match(family, str(item.get("family") or "")):
                return True
            if self.families_match(family, self._family_from_name(str(item.get("name") or ""))[0]):
                return True
        return False

    @staticmethod
    def rotation_badge(count: int) -> Dict[str, Any]:
        value = max(0, int(count or 0))
        return {
            "rotated_today": value > 0,
            "rotation_count": value,
            "rotation_label": f"今日已轮 {value}次" if value else "今日未轮",
            "rotation_tone": "ok" if value else "warn",
        }

    def _today_manual_count(self, card: Dict[str, Any], today) -> Optional[int]:
        raw_on = card.get("rotation_manual_on")
        if raw_on in (None, ""):
            return None
        if hasattr(raw_on, "date"):
            on = raw_on.date().isoformat()
        elif hasattr(raw_on, "isoformat"):
            on = raw_on.isoformat()[:10]
        else:
            on = str(raw_on).strip()[:10]
        if on != today.isoformat():
            return None
        raw_count = card.get("rotation_manual_count")
        if raw_count in (None, ""):
            return None
        try:
            return max(0, int(raw_count))
        except (TypeError, ValueError):
            return None

    def annotate_rotation(
        self,
        boxes: List[Dict[str, Any]],
        cards: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        today = (now or get_now()).date()
        for box in boxes:
            emails: set[str] = set()
            team_id = None
            manual = None
            for card in cards:
                if not self._box_matches_card(box, card):
                    continue
                if team_id is None and card.get("id") is not None:
                    try:
                        team_id = int(card["id"])
                    except (TypeError, ValueError):
                        team_id = None
                if manual is None:
                    manual = self._today_manual_count(card, today)
                owner = str(card.get("email") or "").strip().lower()
                for item in card.get("rotation_emails") or []:
                    email = str(item or "").strip().lower()
                    if email and email != owner:
                        emails.add(email)
                members = list(card.get("live_members") or []) + list(card.get("active_children") or [])
                for member in members:
                    if str(member.get("role") or "") == "account-owner":
                        continue
                    email = str(member.get("email") or "").strip().lower()
                    if email and email == owner:
                        continue
                    if self._is_today(member.get("joined_at") or member.get("added_at"), today):
                        if email:
                            emails.add(email)
            auto = len(emails)
            count = auto if manual is None else manual
            box.update(self.rotation_badge(count))
            box["rotation_auto_count"] = auto
            box["rotation_manual"] = manual is not None
            box["team_id"] = team_id
        return boxes

    async def _login_headers(self, client: httpx.AsyncClient, cfg: Dict[str, Any]) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["x-api-key"] = cfg["api_key"]
            return headers
        if not cfg.get("email") or not cfg.get("password"):
            raise RuntimeError("尚未配置 Sub2API Admin API Key 或后台账号")
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": cfg["email"], "password": cfg["password"]},
        )
        response.raise_for_status()
        data = self._unwrap(response.json())
        token = ""
        if isinstance(data, dict):
            token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("Sub2API 登录成功但没有 access_token")
        headers["Authorization"] = f"Bearer {token}"
        return headers

    async def list_status_accounts(self, db_session: AsyncSession) -> List[Dict[str, Any]]:
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        if not cfg["api_key"] and not (cfg["email"] and cfg["password"]):
            raise RuntimeError("尚未配置 Sub2API Admin API Key 或后台账号")

        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=20.0) as client:
            headers = await self._login_headers(client, cfg)
            seen: Dict[int, Dict[str, Any]] = {}
            searches = ("Team", "2026", "Pedro")
            group_ids = cfg["group_ids"] or [None]
            async def pull(search: str, group_id: Optional[int]) -> List[Dict[str, Any]]:
                params: Dict[str, Any] = {"search": search, "page": 1, "page_size": 100, "sort_by": "name"}
                if group_id is not None:
                    params["group"] = group_id
                response = await client.get("/api/v1/admin/accounts", headers=headers, params=params)
                response.raise_for_status()
                return self._account_items(self._unwrap(response.json()))

            chunks = await asyncio.gather(
                *[pull(search, group_id) for search in searches for group_id in group_ids],
                return_exceptions=True,
            )
            for chunk in chunks:
                if isinstance(chunk, Exception):
                    logger.warning("读取 Sub2API 账号列表失败: %s", chunk)
                    continue
                for item in chunk:
                    account_id = item.get("id")
                    if isinstance(account_id, int):
                        seen[account_id] = item
            if not seen:
                page = 1
                while page <= 5:
                    response = await client.get(
                        "/api/v1/admin/accounts",
                        headers=headers,
                        params={"page": page, "page_size": 100, "sort_by": "name"},
                    )
                    response.raise_for_status()
                    payload = self._unwrap(response.json())
                    items = self._account_items(payload)
                    if not items:
                        break
                    for item in items:
                        account_id = item.get("id")
                        if isinstance(account_id, int):
                            seen[account_id] = item
                    total = payload.get("total") if isinstance(payload, dict) else None
                    if total is not None and len(seen) >= int(total):
                        break
                    page += 1
        return [item for item in seen.values() if self._is_relevant_account(item)]

    def _status_cache_key(self, cfg: Dict[str, Any]) -> str:
        return f"{cfg['base_url']}|{cfg['api_key']}|{cfg['email']}|{','.join(str(i) for i in cfg['group_ids'])}"

    @staticmethod
    def format_usd(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return f"{number:.2f}"

    def parse_cost(self, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def cost_fields(
        self,
        account_cost: Any = None,
        user_cost: Any = None,
        *,
        prefix: str = "",
    ) -> Dict[str, Any]:
        account = self.parse_cost(account_cost)
        user = self.parse_cost(user_cost)
        return {
            f"{prefix}account_cost": account,
            f"{prefix}user_cost": user,
            f"{prefix}account_cost_label": f"A ${usd}" if (usd := self.format_usd(account)) is not None else "",
            f"{prefix}user_cost_label": f"U ${usd}" if (usd := self.format_usd(user)) is not None else "",
        }

    def sum_costs(
        self,
        rows: List[Dict[str, Any]],
        account_key: str = "account_cost",
        user_key: str = "user_cost",
    ) -> tuple[Optional[float], Optional[float]]:
        account_total = 0.0
        user_total = 0.0
        has_account = False
        has_user = False
        for row in rows:
            account = self.parse_cost(row.get(account_key))
            user = self.parse_cost(row.get(user_key))
            if account is not None:
                account_total += account
                has_account = True
            if user is not None:
                user_total += user
                has_user = True
        return (
            round(account_total, 2) if has_account else None,
            round(user_total, 2) if has_user else None,
        )

    def annotate_cost_totals(self, boxes: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_rows: List[Dict[str, Any]] = []
        for box in boxes:
            rows = list(box.get("accounts") or [])
            all_rows.extend(rows)
            box.update(self.cost_fields(*self.sum_costs(rows)))
            box.update(self.cost_fields(
                *self.sum_costs(rows, "lifetime_account_cost", "lifetime_user_cost"),
                prefix="lifetime_",
            ))
        totals = self.cost_fields(*self.sum_costs(all_rows))
        totals.update(self.cost_fields(
            *self.sum_costs(all_rows, "lifetime_account_cost", "lifetime_user_cost"),
            prefix="lifetime_",
        ))
        return totals

    def usage_ledger_key(self, row: Dict[str, Any]) -> str:
        email = str(row.get("email") or "").strip().lower()
        if email:
            return f"email:{email}"
        try:
            return f"id:{int(row['id'])}"
        except (TypeError, ValueError, KeyError):
            name = str(row.get("name") or "").strip().lower()
            return f"name:{name}" if name else ""

    def advance_cost(
        self,
        last: Any,
        lifetime: Any,
        current: Any,
    ) -> tuple[Optional[float], Optional[float]]:
        current_value = self.parse_cost(current)
        last_value = self.parse_cost(last)
        lifetime_value = self.parse_cost(lifetime)
        if current_value is None:
            return last_value, lifetime_value
        current_value = round(current_value, 2)
        last_value = round(last_value or 0.0, 2)
        lifetime_value = round(lifetime_value or 0.0, 2)
        if current_value >= last_value:
            lifetime_value = round(lifetime_value + current_value - last_value, 2)
        return current_value, lifetime_value

    async def apply_lifetime_costs(
        self,
        db_session: AsyncSession,
        rows: List[Dict[str, Any]],
    ) -> None:
        keyed: List[tuple[str, Dict[str, Any]]] = []
        for row in rows:
            key = self.usage_ledger_key(row)
            if key:
                keyed.append((key, row))
        if not keyed:
            return
        result = await db_session.execute(
            select(Sub2ApiUsageLedger).where(
                Sub2ApiUsageLedger.ledger_key.in_([key for key, _ in keyed])
            )
        )
        existing = {item.ledger_key: item for item in result.scalars().all()}
        now = get_now()
        for key, row in keyed:
            item = existing.get(key)
            if item is None:
                item = Sub2ApiUsageLedger(ledger_key=key, created_at=now)
                db_session.add(item)
                existing[key] = item
            last_account, lifetime_account = self.advance_cost(
                item.last_account_cost,
                item.lifetime_account_cost,
                row.get("account_cost"),
            )
            last_user, lifetime_user = self.advance_cost(
                item.last_user_cost,
                item.lifetime_user_cost,
                row.get("user_cost"),
            )
            item.last_account_cost = last_account
            item.last_user_cost = last_user
            item.lifetime_account_cost = lifetime_account
            item.lifetime_user_cost = lifetime_user
            item.updated_at = now
            email = str(row.get("email") or "").strip().lower()
            if email:
                item.email = email
            try:
                item.sub2api_account_id = int(row["id"])
            except (TypeError, ValueError, KeyError):
                pass
            family = str(row.get("family") or "").strip()
            if family:
                item.family = family
            row.update(self.cost_fields(lifetime_account, lifetime_user, prefix="lifetime_"))
        await db_session.flush()

    def apply_window_costs(self, row: Dict[str, Any], usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = usage if isinstance(usage, dict) else {}
        seven = payload.get("seven_day")
        stats = seven.get("window_stats") if isinstance(seven, dict) else None
        if not isinstance(stats, dict):
            stats = {}
        row.update(self.cost_fields(stats.get("cost"), stats.get("user_cost")))
        return row

    def _usage_payload_map(self, data: Any) -> Dict[int, Dict[str, Any]]:
        payload_by_id: Dict[int, Dict[str, Any]] = {}
        if not isinstance(data, dict):
            return payload_by_id
        usage_map = data.get("usage") if isinstance(data.get("usage"), dict) else data
        if not isinstance(usage_map, dict):
            return payload_by_id
        for key, payload in usage_map.items():
            try:
                payload_by_id[int(key)] = payload if isinstance(payload, dict) else {}
            except (TypeError, ValueError):
                continue
        return payload_by_id

    def has_local_rate_limit_lock(self, account: Dict[str, Any]) -> bool:
        """Sub 本地「限流中 / 429」锁：reset 还在未来，或仍记着 rate_limited_at。"""
        if self._is_future(account.get("rate_limit_reset_at")):
            return True
        return account.get("rate_limited_at") not in (None, "")

    def merge_usage_into_account(
        self,
        account: Dict[str, Any],
        usage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把 usage 探测写回 extra，再重算调度标签。

        兼容 usage/batch 的 extra 字段，以及官方查询的 seven_day / five_hour。
        """
        merged = dict(account or {})
        extra = dict(self._account_extra(merged))
        payload = usage if isinstance(usage, dict) else {}
        nested_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        mapped: Dict[str, Any] = {}
        seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
        five = payload.get("five_hour") if isinstance(payload.get("five_hour"), dict) else {}
        if seven.get("utilization") not in (None, ""):
            mapped["codex_7d_used_percent"] = seven["utilization"]
        if seven.get("resets_at") not in (None, ""):
            mapped["codex_7d_reset_at"] = seven["resets_at"]
        if five.get("utilization") not in (None, ""):
            mapped["codex_5h_used_percent"] = five["utilization"]
        if five.get("resets_at") not in (None, ""):
            mapped["codex_5h_reset_at"] = five["resets_at"]
        for source in (payload, nested_extra, mapped):
            if not isinstance(source, dict):
                continue
            for key in (
                "codex_7d_used_percent",
                "codex_7d_reset_at",
                "codex_secondary_reset_at",
                "codex_5h_used_percent",
                "codex_primary_used_percent",
                "codex_5h_reset_at",
                "codex_primary_reset_at",
            ):
                if source.get(key) not in (None, ""):
                    extra[key] = source[key]
            if source.get("error_message") not in (None, ""):
                merged["error_message"] = source["error_message"]
            if source.get("status") not in (None, ""):
                merged["status"] = source["status"]
            if "schedulable" in source:
                merged["schedulable"] = source["schedulable"]
            for key in ("rate_limit_reset_at", "rate_limited_at"):
                if key in source:
                    merged[key] = source[key]
        if extra:
            merged["extra"] = extra
        return merged

    async def fetch_usage_batch(
        self,
        db_session: AsyncSession,
        account_ids: List[int],
        *,
        force: bool = False,
    ) -> Dict[int, Dict[str, Any]]:
        ids: List[int] = []
        seen: set[int] = set()
        for raw in account_ids:
            try:
                account_id = int(raw)
            except (TypeError, ValueError):
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            ids.append(account_id)
        if not ids:
            return {}
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        payload_by_id: Dict[int, Dict[str, Any]] = {}
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=30.0) as client:
            headers = await self._login_headers(client, cfg)
            response = await client.post(
                "/api/v1/admin/accounts/usage/batch",
                headers=headers,
                json={"account_ids": ids, "force": bool(force)},
            )
            response.raise_for_status()
            payload_by_id = self._usage_payload_map(self._unwrap(response.json()))
        return payload_by_id

    async def fetch_account_usage(
        self,
        db_session: AsyncSession,
        account_id: int,
        *,
        source: str = "active",
        force: bool = True,
    ) -> Dict[str, Any]:
        """官方「查询」：GET /admin/accounts/{id}/usage?source=active&force=true。"""
        if not account_id:
            return {}
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        params: Dict[str, str] = {}
        if source:
            params["source"] = str(source)
        if force:
            params["force"] = "true"
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=60.0) as client:
            headers = await self._login_headers(client, cfg)
            response = await client.get(
                f"/api/v1/admin/accounts/{int(account_id)}/usage",
                headers=headers,
                params=params or None,
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        return data if isinstance(data, dict) else {}

    async def clear_account_rate_limit(
        self,
        db_session: AsyncSession,
        account_id: int,
    ) -> Dict[str, Any]:
        """清掉 Sub 本地限流锁，对应后台 clear-rate-limit。"""
        if not account_id:
            return {}
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=30.0) as client:
            headers = await self._login_headers(client, cfg)
            response = await client.post(
                f"/api/v1/admin/accounts/{int(account_id)}/clear-rate-limit",
                headers=headers,
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        return data if isinstance(data, dict) else {}

    async def get_account(
        self,
        db_session: AsyncSession,
        account_id: int,
    ) -> Dict[str, Any]:
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=20.0) as client:
            headers = await self._login_headers(client, cfg)
            response = await client.get(f"/api/v1/admin/accounts/{account_id}", headers=headers)
            response.raise_for_status()
            data = self._unwrap(response.json())
        return data if isinstance(data, dict) else {}

    async def patch_account_fields(
        self,
        db_session: AsyncSession,
        account_id: int,
        patch: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not account_id or not patch:
            return {"account": {}, "patched": False, "patch": patch or {}}
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=20.0) as client:
            headers = await self._login_headers(client, cfg)
            response = await client.put(
                f"/api/v1/admin/accounts/{account_id}",
                headers=headers,
                json=patch,
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        account = data if isinstance(data, dict) else {}
        return {"account": account, "patched": True, "patch": patch}

    async def attach_usage_costs(
        self,
        db_session: AsyncSession,
        boxes: List[Dict[str, Any]],
        *,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        rows = [account for box in boxes for account in (box.get("accounts") or []) if account.get("id") is not None]
        if not rows:
            return boxes

        async def finish() -> List[Dict[str, Any]]:
            try:
                await self.apply_lifetime_costs(db_session, rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning("累计消费记账失败: %s", exc)
            return boxes
        now = time.monotonic()
        missing: List[Dict[str, Any]] = []
        for row in rows:
            try:
                account_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            cached = _USAGE_COST_CACHE.get(account_id)
            if not force and cached and now - cached[0] < _USAGE_COST_TTL:
                self.apply_window_costs(row, cached[1])
            else:
                missing.append(row)
        if not missing:
            return await finish()
        cfg = await self._config(db_session)
        if not cfg["base_url"]:
            return await finish()
        ids: List[int] = []
        seen: set[int] = set()
        for row in missing:
            try:
                account_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            ids.append(account_id)
        payload_by_id: Dict[int, Dict[str, Any]] = {}
        async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=20.0) as client:
            headers = await self._login_headers(client, cfg)
            for offset in range(0, len(ids), 80):
                chunk = ids[offset:offset + 80]
                try:
                    response = await client.post(
                        "/api/v1/admin/accounts/usage/batch",
                        headers=headers,
                        json={"account_ids": chunk, "force": False},
                    )
                    response.raise_for_status()
                    data = self._unwrap(response.json())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("批量读取 7日消费失败: %s", exc)
                    continue
                usage_map = data.get("usage") if isinstance(data, dict) else None
                if not isinstance(usage_map, dict):
                    continue
                for key, payload in usage_map.items():
                    try:
                        payload_by_id[int(key)] = payload if isinstance(payload, dict) else {}
                    except (TypeError, ValueError):
                        continue
        stamp = time.monotonic()
        for row in missing:
            try:
                account_id = int(row["id"])
            except (TypeError, ValueError):
                continue
            payload = payload_by_id.get(account_id, {})
            _USAGE_COST_CACHE[account_id] = (stamp, payload)
            self.apply_window_costs(row, payload)
        return await finish()

    async def dashboard_status(
        self,
        db_session: AsyncSession,
        *,
        force: bool = False,
        allow_network: bool = True,
    ) -> Dict[str, Any]:
        cfg = await self._config(db_session)
        configured = bool(cfg["base_url"] and (cfg["api_key"] or (cfg["email"] and cfg["password"])))
        cache_key = self._status_cache_key(cfg)
        cached = _STATUS_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and not force and (now - cached[0] < _STATUS_CACHE_TTL or not allow_network):
            return cached[1]
        if not allow_network:
            return {
                "ok": configured,
                "configured": configured,
                "boxes": [],
                "count": 0,
                "error": None if configured else "还没配 Sub2API",
                "pending": configured,
            }
        try:
            accounts = await self.list_status_accounts(db_session)
            boxes = self.group_accounts(accounts)
            result = {
                "ok": True,
                "configured": True,
                "boxes": boxes,
                "count": sum(len(box["accounts"]) for box in boxes),
                "error": None,
                "pending": False,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Sub2API 状态失败: %s", exc)
            result = {
                "ok": False,
                "configured": configured,
                "boxes": [],
                "count": 0,
                "error": str(exc) or type(exc).__name__,
                "pending": False,
            }
        _STATUS_CACHE[cache_key] = (now, result)
        return result

    async def ensure_account_runtime(
        self,
        client: httpx.AsyncClient,
        headers: Dict[str, str],
        account_id: Optional[int],
        *,
        proxy_id: Optional[int] = None,
        group_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        if not account_id:
            return {}
        account: Dict[str, Any] = {}
        try:
            response = await client.get(f"/api/v1/admin/accounts/{account_id}", headers=headers)
            if response.status_code < 400:
                data = self._unwrap(response.json())
                if isinstance(data, dict):
                    account = data
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Sub2API 账号 %s 失败: %s", account_id, exc)
        patch: Dict[str, Any] = {}
        if proxy_id and not self._account_proxy_id(account):
            patch["proxy_id"] = proxy_id
        wanted_groups = self._positive_ids(group_ids or [])
        if wanted_groups and not self._account_group_ids(account):
            patch["group_ids"] = wanted_groups
            patch["confirm_mixed_channel_risk"] = True
        if not patch:
            return {"account": account, "patched": False}
        try:
            response = await client.put(
                f"/api/v1/admin/accounts/{account_id}",
                headers=headers,
                json=patch,
            )
            if response.status_code < 400:
                data = self._unwrap(response.json())
                if isinstance(data, dict):
                    account = data
                return {"account": account, "patched": True, "patch": patch}
            logger.warning("回填 Sub2API 账号 %s 失败: %s %s", account_id, response.status_code, response.text[:240])
        except Exception as exc:  # noqa: BLE001
            logger.warning("回填 Sub2API 账号 %s 失败: %s", account_id, exc)
        return {"account": account, "patched": False, "patch": patch}

    async def probe_access_token(
        self,
        db_session: AsyncSession,
        access_token: str,
        *,
        email: str = "",
        proxy_url: str = "",
    ) -> Dict[str, Any]:
        if not access_token:
            return self.classify_probe(status_code=None, error="", payload=None)
        from app.services.chatgpt import chatgpt_service

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            result = await chatgpt_service._make_request(
                "GET",
                f"{chatgpt_service.BASE_URL}/me",
                headers,
                db_session=db_session,
                identifier=email or "probe",
            )
        except Exception as exc:  # noqa: BLE001
            return self.classify_probe(status_code=0, error=str(exc))
        probe = self.classify_probe(
            status_code=result.get("status_code"),
            error=str(result.get("error") or ""),
            payload=result.get("data") if isinstance(result.get("data"), dict) else result,
        )
        probe["status_code"] = result.get("status_code")
        probe["detail"] = str(result.get("error") or "")[:240]
        if proxy_url:
            probe["proxy"] = self.proxy_endpoint_from_url(proxy_url)
        return probe

    async def import_session(
            self,
            db_session: AsyncSession,
            *,
            email: str,
            access_token: str,
            refresh_token: str = "",
            id_token: str = "",
            account_id: str = "",
            client_id: str = "",
            existing_id: Optional[int] = None,
            team: Any = None,
            proxy_url: str = "",
            role: str = "child",
            template_name: str = "",
            name_style: str = "",
        ) -> Dict[str, Any]:
            cfg = await self._config(db_session)
            if not cfg["base_url"]:
                raise RuntimeError("尚未配置 Sub2API 地址")
            if not cfg["api_key"] and not (cfg["email"] and cfg["password"]):
                raise RuntimeError("尚未配置 Sub2API Admin API Key 或后台账号")
            if not access_token:
                raise RuntimeError("缺少 access token，无法推送 Sub2API")

            team_email = str(getattr(team, "email", "") or "") if team is not None else ""
            team_name = str(getattr(team, "team_name", "") or "") if team is not None else ""
            proxy_url = proxy_url or (str(getattr(team, "proxy", "") or "") if team is not None else "")
            role = "owner" if role == "owner" else "child"

            errors: list[str] = []
            async with httpx.AsyncClient(base_url=cfg["base_url"], timeout=60.0) as client:
                headers = await self._login_headers(client, cfg)

                async def admin_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
                    response = await client.get(path, headers=headers, params=params)
                    response.raise_for_status()
                    return self._unwrap(response.json())

                async def finish(result: Dict[str, Any]) -> Dict[str, Any]:
                    ensured = await self.ensure_account_runtime(
                        client,
                        headers,
                        result.get("account_id"),
                        proxy_id=session_payload.get("proxy_id"),
                        group_ids=session_payload.get("group_ids") or [],
                    )
                    if ensured.get("patched") and ensured.get("account"):
                        result["runtime"] = {"patched": True, "patch": ensured.get("patch")}
                    result["probe"] = await self.probe_access_token(
                        db_session,
                        access_token,
                        email=email,
                        proxy_url=proxy_url,
                    )
                    invalidate_status_cache()
                    return result

                templates: List[Dict[str, Any]] = []
                try:
                    payload = await admin_get("/api/v1/admin/settings/account-create-templates")
                    items = payload.get("items") if isinstance(payload, dict) else payload
                    if isinstance(items, list):
                        templates = [item for item in items if isinstance(item, dict)]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取 Sub2API 账号模板失败: %s", exc)

                proxies: List[Dict[str, Any]] = []
                try:
                    payload = await admin_get("/api/v1/admin/proxies", {"page": 1, "page_size": 200})
                    items = payload.get("items") if isinstance(payload, dict) else payload
                    if isinstance(items, list):
                        proxies = [item for item in items if isinstance(item, dict)]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取 Sub2API 代理列表失败: %s", exc)

                searches = ["Team", "2026", "Pedro"]
                if name_style == "free":
                    searches.append("Free")
                for extra in (team_email.split("@", 1)[0], self.derive_family_label(team_email, team_name), email.split("@", 1)[0]):
                    extra = (extra or "").strip()
                    if extra and extra not in searches:
                        searches.append(extra)
                accounts: List[Dict[str, Any]] = []
                seen: Dict[int, Dict[str, Any]] = {}
                try:
                    for search in searches:
                        payload = await admin_get(
                            "/api/v1/admin/accounts",
                            {"search": search, "page": 1, "page_size": 100, "sort_by": "name"},
                        )
                        for item in self._account_items(payload):
                            account_pk = item.get("id")
                            if isinstance(account_pk, int) and account_pk not in seen:
                                seen[account_pk] = item
                    accounts = list(seen.values())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取 Sub2API 账号列表失败: %s", exc)

                wanted_template = (template_name or "").strip()
                if not wanted_template:
                    wanted_template = cfg.get("free_template_name") if name_style == "free" else cfg.get("template_name")
                template = self.pick_account_create_template(templates, name=wanted_template or "") if role == "child" else None
                fallback_groups = [] if name_style == "free" else list(cfg["group_ids"] or [])
                template_fields = self.template_import_fields(template, fallback_groups) if role == "child" else {
                    "name": "",
                    "group_ids": list(cfg["group_ids"] or []),
                    "concurrency": None,
                    "priority": None,
                    "rate_multiplier": None,
                    "load_factor": None,
                    "auto_pause_on_expired": None,
                    "extra": None,
                }
                existing = self.find_existing_account(accounts, email=email, existing_id=existing_id)
                siblings = self.family_accounts(accounts, team_email or email, team_name)
                family_label = self.derive_family_label(team_email or email, team_name)
                proxy_id = self.match_proxy_id(proxies, proxy_url, siblings, family_label=family_label)
                if name_style == "free":
                    account_name = self.build_free_account_name(email=email, accounts=accounts)
                else:
                    account_name = self.build_account_name(
                        role=role,
                        email=email,
                        team_email=team_email or email,
                        team_name=team_name,
                        accounts=accounts,
                    )
                session_payload = self.build_codex_import_payload(
                    content=access_token,
                    name=account_name,
                    role=role,
                    existing=existing,
                    template_fields=template_fields,
                    fallback_group_ids=fallback_groups,
                    proxy_id=proxy_id,
                )
                logger.info(
                    "推送 Sub2API: email=%s name=%s role=%s template=%s proxy_id=%s create=%s",
                    email,
                    account_name,
                    role,
                    template_fields.get("name") or "-",
                    session_payload.get("proxy_id"),
                    existing is None,
                )
                existing_pk = existing_id or (existing or {}).get("id")
                try:
                    existing_pk = int(existing_pk) if existing_pk else None
                except (TypeError, ValueError):
                    existing_pk = None
                if existing_pk:
                    apply_payload = {
                        "type": "oauth",
                        "credentials": self.build_oauth_credentials(
                            email=email,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            id_token=id_token,
                            account_id=account_id,
                            client_id=client_id,
                        ),
                        "extra": {"email": email} if email else {},
                    }
                    try:
                        response = await client.post(
                            f"/api/v1/admin/accounts/{existing_pk}/apply-oauth-credentials",
                            headers=headers,
                            json=apply_payload,
                        )
                        if response.status_code < 400:
                            data = self._unwrap(response.json())
                            return await finish({
                                "strategy": "apply_oauth_credentials",
                                "account_id": self._extract_account_id(data) or existing_pk,
                                "data": data,
                                "name": account_name,
                                "proxy_id": session_payload.get("proxy_id"),
                                "template": template_fields.get("name") or None,
                            })
                        errors.append(f"apply_oauth_credentials: {response.status_code} {response.text[:240]}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"apply_oauth_credentials: {exc}")

                try:
                    response = await client.post(
                        "/api/v1/admin/accounts/import/codex-session",
                        headers=headers,
                        json=session_payload,
                    )
                    if response.status_code < 400:
                        data = self._unwrap(response.json())
                        account_pk = self._extract_account_id(data) or existing_id or (existing or {}).get("id")
                        try:
                            account_pk = int(account_pk) if account_pk else None
                        except (TypeError, ValueError):
                            account_pk = None
                        if refresh_token and account_pk:
                            apply_payload = {
                                "type": "oauth",
                                "credentials": self.build_oauth_credentials(
                                    email=email,
                                    access_token=access_token,
                                    refresh_token=refresh_token,
                                    id_token=id_token,
                                    account_id=account_id,
                                    client_id=client_id,
                                ),
                                "extra": {"email": email} if email else {},
                            }
                            try:
                                apply_response = await client.post(
                                    f"/api/v1/admin/accounts/{account_pk}/apply-oauth-credentials",
                                    headers=headers,
                                    json=apply_payload,
                                )
                                if apply_response.status_code < 400:
                                    data = self._unwrap(apply_response.json())
                                    return await finish({
                                        "strategy": "import_codex_session+apply_oauth_credentials",
                                        "account_id": self._extract_account_id(data) or account_pk,
                                        "data": data,
                                        "name": account_name,
                                        "proxy_id": session_payload.get("proxy_id"),
                                        "template": template_fields.get("name") or None,
                                    })
                                errors.append(
                                    f"apply_oauth_credentials_after_import: {apply_response.status_code} {apply_response.text[:240]}"
                                )
                            except Exception as exc:  # noqa: BLE001
                                errors.append(f"apply_oauth_credentials_after_import: {exc}")
                        return await finish({
                            "strategy": "import_codex_session",
                            "account_id": account_pk or existing_id or (existing or {}).get("id"),
                            "data": data,
                            "name": account_name,
                            "proxy_id": session_payload.get("proxy_id"),
                            "template": template_fields.get("name") or None,
                        })
                    errors.append(f"import_codex_session: {response.status_code} {response.text[:240]}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"import_codex_session: {exc}")

                credentials = self.build_oauth_credentials(
                    email=email,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    id_token=id_token,
                    account_id=account_id,
                    client_id=client_id,
                )

                create_payload = {
                    "name": account_name,
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": credentials,
                    "status": "active",
                }
                for key in ("group_ids", "proxy_id", "concurrency", "priority", "rate_multiplier", "load_factor", "extra", "confirm_mixed_channel_risk", "auto_pause_on_expired"):
                    if key in session_payload:
                        create_payload[key] = session_payload[key]
                try:
                    response = await client.post("/api/v1/admin/accounts", headers=headers, json=create_payload)
                    if response.status_code < 400:
                        data = self._unwrap(response.json())
                        return await finish({
                            "strategy": "create_account",
                            "account_id": self._extract_account_id(data) or existing_id,
                            "data": data,
                            "name": account_name,
                            "proxy_id": session_payload.get("proxy_id"),
                            "template": template_fields.get("name") or None,
                        })
                    errors.append(f"create_account: {response.status_code} {response.text[:240]}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"create_account: {exc}")

            raise RuntimeError("Sub2API 推送失败: " + " | ".join(errors))

    async def push_team(self, db_session: AsyncSession, team: Any) -> Dict[str, Any]:
        from app.services.encryption import encryption_service

        def _decrypt(value: Optional[str]) -> str:
            if not value:
                return ""
            try:
                return encryption_service.decrypt_token(value)
            except Exception as exc:
                logger.warning("解密 Team %s 凭证失败: %s", getattr(team, "id", "?"), exc)
                return ""

        access_token = _decrypt(getattr(team, "access_token_encrypted", None))
        if not access_token:
            return {"success": False, "error": "Team 缺少 Access Token，无法推送到 Sub2API", "email": getattr(team, "email", "")}

        result = await self.import_session(
            db_session,
            email=str(getattr(team, "email", "") or ""),
            access_token=access_token,
            refresh_token=_decrypt(getattr(team, "refresh_token_encrypted", None)),
            id_token=_decrypt(getattr(team, "id_token_encrypted", None)),
            account_id=str(getattr(team, "account_id", "") or ""),
            client_id=str(getattr(team, "client_id", "") or ""),
            team=team,
            proxy_url=str(getattr(team, "proxy", "") or ""),
            role="owner",
        )
        email = str(getattr(team, "email", "") or "")
        probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
        probe_label = str(probe.get("label") or "").strip()
        message = f"已推送到 Sub2API：{email}"
        if probe_label:
            message += f"，探测 {probe_label}"
        if result.get("runtime", {}).get("patched"):
            message += "，已回填缺失的代理/分组"
        warning = None
        if probe.get("kind") == "phone":
            warning = "账号未接码，推送不会覆盖已有代理/分组，可在子号池用本机认证补票"
        elif probe.get("kind") in {"401", "403"}:
            warning = f"推送后探测为 {probe_label}，Token 可能还不能用"
        return {
            "success": True,
            "message": message,
            "email": email,
            "filename": email,
            "action": "updated" if result.get("strategy") == "create_account" else "uploaded",
            "account_id": result.get("account_id"),
            "proxy_id": result.get("proxy_id"),
            "template": result.get("template"),
            "probe": probe,
            "warning": warning,
            "warnings": [warning] if warning else [],
        }


sub2api_service = Sub2ApiService()
