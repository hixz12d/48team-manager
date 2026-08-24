"""Sub2API Admin 导入与只读状态。同机部署，不走代理。"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings import settings_service

logger = logging.getLogger(__name__)

_TEAM_PREFIX_RE = re.compile(r"^(?:GPT\s+)?Team\s*", re.IGNORECASE)
_CHILD_RE = re.compile(r"子号\s*(\d+)")
_EMAIL_FAMILY_RE = re.compile(r"(?:xiaozhudf\.?)?(\d{4}\.\d+)", re.IGNORECASE)
_STATUS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STATUS_CACHE_TTL = 45.0


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
        return {
            "base_url": base_url or "http://sub2api-canary:8080",
            "api_key": api_key,
            "email": email,
            "password": password,
            "group_ids": group_ids,
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

    def _quota_percent(self, account: Dict[str, Any]) -> Optional[int]:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        for key in ("codex_7d_used_percent", "codex_primary_used_percent", "codex_5h_used_percent"):
            raw = extra.get(key)
            if raw in (None, ""):
                continue
            try:
                return max(0, min(100, int(round(float(raw)))))
            except (TypeError, ValueError):
                continue
        return None

    def _schedule_state(self, account: Dict[str, Any]) -> Dict[str, str]:
        error = str(account.get("error_message") or "").strip()
        lowered = error.lower()
        reset_at = account.get("rate_limit_reset_at")
        limited = bool(account.get("rate_limited_at")) or self._is_future(reset_at)
        if "401" in error or "revoked" in lowered or "invalidated oauth" in lowered:
            return {"kind": "401", "label": "401 失效", "tone": "danger"}
        if limited or "429" in error or "rate limit" in lowered:
            remain = self._format_remain(reset_at)
            label = f"429 {remain}" if remain else "429 限额"
            return {"kind": "429", "label": label, "tone": "warn"}
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
        }

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
            for search in searches:
                for group_id in group_ids:
                    params: Dict[str, Any] = {"search": search, "page": 1, "page_size": 100, "sort_by": "name"}
                    if group_id is not None:
                        params["group"] = group_id
                    response = await client.get("/api/v1/admin/accounts", headers=headers, params=params)
                    response.raise_for_status()
                    for item in self._account_items(self._unwrap(response.json())):
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
        return [
            item for item in seen.values()
            if "team" in str(item.get("name") or "").lower()
        ]

    async def dashboard_status(self, db_session: AsyncSession) -> Dict[str, Any]:
        cfg = await self._config(db_session)
        cache_key = f"{cfg['base_url']}|{cfg['api_key']}|{cfg['email']}|{','.join(str(i) for i in cfg['group_ids'])}"
        cached = _STATUS_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < _STATUS_CACHE_TTL:
            return cached[1]
        try:
            accounts = await self.list_status_accounts(db_session)
            boxes = self.group_accounts(accounts)
            result = {
                "ok": True,
                "configured": True,
                "boxes": boxes,
                "count": sum(len(box["accounts"]) for box in boxes),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Sub2API 状态失败: %s", exc)
            result = {
                "ok": False,
                "configured": bool(cfg["api_key"] or (cfg["email"] and cfg["password"])),
                "boxes": [],
                "count": 0,
                "error": str(exc) or type(exc).__name__,
            }
        _STATUS_CACHE[cache_key] = (now, result)
        return result

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
    ) -> Dict[str, Any]:
        cfg = await self._config(db_session)
        if not cfg["base_url"] or not cfg["api_key"]:
            raise RuntimeError("尚未配置 Sub2API 地址或 Admin API Key")
        if not access_token:
            raise RuntimeError("缺少 access token，无法推送 Sub2API")

        headers = {
            "x-api-key": cfg["api_key"],
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        errors: list[str] = []
        async with httpx.AsyncClient(base_url=cfg["base_url"], headers=headers, timeout=60.0) as client:
            session_payload = {"content": access_token, "name": email}
            if cfg["group_ids"]:
                session_payload["group_ids"] = cfg["group_ids"]
            try:
                response = await client.post("/api/v1/admin/accounts/import/codex-session", json=session_payload)
                if response.status_code < 400:
                    data = self._unwrap(response.json())
                    return {
                        "strategy": "import_codex_session",
                        "account_id": self._extract_account_id(data) or existing_id,
                        "data": data,
                    }
                errors.append(f"import_codex_session: {response.status_code} {response.text[:240]}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"import_codex_session: {exc}")

            credentials = {
                "access_token": access_token,
                "email": email,
            }
            if refresh_token:
                credentials["refresh_token"] = refresh_token
            if id_token:
                credentials["id_token"] = id_token
            if account_id:
                credentials["chatgpt_account_id"] = account_id
            if client_id:
                credentials["client_id"] = client_id

            create_payload = {
                "name": email,
                "platform": "openai",
                "type": "oauth",
                "credentials": credentials,
                "group_ids": cfg["group_ids"],
                "status": "active",
            }
            try:
                response = await client.post("/api/v1/admin/accounts", json=create_payload)
                if response.status_code < 400:
                    data = self._unwrap(response.json())
                    return {
                        "strategy": "create_account",
                        "account_id": self._extract_account_id(data) or existing_id,
                        "data": data,
                    }
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
        )
        email = str(getattr(team, "email", "") or "")
        return {
            "success": True,
            "message": f"已推送到 Sub2API：{email}",
            "email": email,
            "filename": email,
            "action": "updated" if result.get("strategy") == "create_account" else "uploaded",
            "account_id": result.get("account_id"),
            "warning": None,
            "warnings": [],
        }


sub2api_service = Sub2ApiService()
