"""Sub2API Admin HTTP. Binding and runtime only; never compose or guess identity from names."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.settings import get_setting_value
from app.domain.identity.ids import normalize_email
from app.domain.quota import clamp_percent

logger = logging.getLogger(__name__)

DEFAULT_SUB2API_BASE_URL = "http://sub2api-canary:8080"


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _parse_when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_future(value: Any) -> bool:
    when = _parse_when(value)
    if when is None:
        return False
    return when > datetime.now(timezone.utc)


class Sub2ApiClient:
    def _unwrap(self, data: Any) -> Any:
        if isinstance(data, dict) and "code" in data:
            if data.get("code") not in (0, "0", None, 200):
                raise RuntimeError(data.get("message") or str(data))
            return data.get("data", data)
        return data

    def _account_items(self, data: Any) -> list[dict[str, Any]]:
        payload = data
        if isinstance(data, dict):
            payload = data.get("items") or data.get("accounts") or data.get("list") or data
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def account_email(self, account: dict[str, Any] | None) -> str:
        payload = account if isinstance(account, dict) else {}
        credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        return normalize_email(
            credentials.get("email") or extra.get("email") or payload.get("email") or ""
        )

    def remote_id(self, account: dict[str, Any] | None) -> int | None:
        payload = account if isinstance(account, dict) else {}
        return _as_int(payload.get("id"))

    def _account_extra(self, account: dict[str, Any]) -> dict[str, Any]:
        extra = account.get("extra")
        return dict(extra) if isinstance(extra, dict) else {}

    def _quota_percent(self, account: dict[str, Any]) -> int | None:
        extra = self._account_extra(account)
        return clamp_percent(extra.get("codex_7d_used_percent"))

    def _five_hour_percent(self, account: dict[str, Any]) -> int | None:
        extra = self._account_extra(account)
        return clamp_percent(extra.get("codex_5h_used_percent") or extra.get("codex_primary_used_percent"))

    def _weekly_reset_at(self, account: dict[str, Any]) -> Any:
        extra = self._account_extra(account)
        return extra.get("codex_7d_reset_at") or extra.get("codex_secondary_reset_at")

    def _five_hour_reset_at(self, account: dict[str, Any]) -> Any:
        extra = self._account_extra(account)
        return extra.get("codex_5h_reset_at") or extra.get("codex_primary_reset_at")

    def _looks_weekly_reset(self, value: Any) -> bool:
        when = _parse_when(value)
        if when is None:
            return False
        delta = when - datetime.now(timezone.utc)
        return delta.total_seconds() >= 2 * 24 * 3600

    def schedule_kind(self, account: dict[str, Any]) -> dict[str, str]:
        error = str(account.get("error_message") or "").strip()
        lowered = error.lower()
        if "phone" in lowered and ("required" in lowered or "sms" in lowered):
            return {"kind": "phone", "label": "未接码", "tone": "warn"}
        if "401" in error or "revoked" in lowered or "invalidated oauth" in lowered:
            return {"kind": "401", "label": "401 失效", "tone": "danger"}
        if "403" in error or "forbidden" in lowered:
            return {"kind": "403", "label": "403", "tone": "danger"}
        weekly_pct = self._quota_percent(account)
        if weekly_pct is not None and weekly_pct >= 100:
            return {"kind": "429", "label": "429 限额", "tone": "warn"}
        five_pct = self._five_hour_percent(account)
        five_reset = self._five_hour_reset_at(account)
        if five_pct is not None and five_pct >= 100 and (not five_reset or _is_future(five_reset)):
            return {"kind": "5h", "label": "5h限制", "tone": "warn"}
        if account.get("status") == "error":
            return {"kind": "error", "label": "异常", "tone": "danger"}
        if account.get("schedulable") is False:
            return {"kind": "paused", "label": "不调度", "tone": "muted"}
        return {"kind": "ok", "label": "可调度", "tone": "ok"}

    def has_local_rate_limit_lock(self, account: dict[str, Any]) -> bool:
        if _is_future(account.get("rate_limit_reset_at")):
            return True
        return account.get("rate_limited_at") not in (None, "")

    def merge_usage_into_account(
        self,
        account: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(account or {})
        extra = dict(self._account_extra(merged))
        payload = usage if isinstance(usage, dict) else {}
        nested_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        mapped: dict[str, Any] = {}
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

    async def load_config(self, db: AsyncSession) -> dict[str, Any]:
        base_url = (await get_setting_value(db, "sub2api_base_url", DEFAULT_SUB2API_BASE_URL) or DEFAULT_SUB2API_BASE_URL).strip().rstrip("/")
        api_key = (await get_setting_value(db, "sub2api_api_key", "") or "").strip()
        email = (await get_setting_value(db, "sub2api_admin_email", "") or "").strip()
        password = (await get_setting_value(db, "sub2api_admin_password", "") or "").strip()
        return {
            "base_url": base_url or DEFAULT_SUB2API_BASE_URL,
            "api_key": api_key,
            "email": email,
            "password": password,
            "configured": bool(api_key or (email and password)),
        }

    async def _login_headers(self, client: httpx.AsyncClient, cfg: dict[str, Any]) -> dict[str, str]:
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

    async def _with_client(self, db: AsyncSession, cfg: dict[str, Any] | None = None):
        cfg = cfg or await self.load_config(db)
        if not cfg["base_url"]:
            raise RuntimeError("尚未配置 Sub2API 地址")
        if not cfg["configured"]:
            raise RuntimeError("尚未配置 Sub2API Admin API Key 或后台账号")
        client = httpx.AsyncClient(base_url=cfg["base_url"], timeout=30.0)
        headers = await self._login_headers(client, cfg)
        return client, headers, cfg

    def account_group_ids(self, account: dict[str, Any] | None) -> list[int]:
        payload = account if isinstance(account, dict) else {}
        ids: list[int] = []
        seen: set[int] = set()

        def add(raw: Any) -> None:
            group_id = _as_int(raw)
            if group_id and group_id not in seen:
                seen.add(group_id)
                ids.append(group_id)

        raw_ids = payload.get("group_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                add(item)
        elif raw_ids not in (None, ""):
            add(raw_ids)
        groups = payload.get("groups")
        if isinstance(groups, list):
            for item in groups:
                if isinstance(item, dict):
                    add(item.get("id"))
                else:
                    add(item)
        add(payload.get("group_id"))
        return ids

    async def _paginate_admin(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        path: str,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        seen: dict[int, dict[str, Any]] = {}
        items_without_id: list[dict[str, Any]] = []
        page = 1
        while page <= 8:
            params = {"page": page, "page_size": 100, **(extra_params or {})}
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            items = self._account_items(self._unwrap(response.json()))
            if not items:
                break
            for item in items:
                item_id = self.remote_id(item)
                if item_id:
                    seen[item_id] = item
                else:
                    items_without_id.append(item)
            if len(items) < 100:
                break
            page += 1
        return list(seen.values()) + items_without_id

    async def list_status_accounts(self, db: AsyncSession, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        client, headers, _cfg = await self._with_client(db, cfg)
        try:
            return await self._paginate_admin(client, headers, "/api/v1/admin/accounts", {"sort_by": "name"})
        finally:
            await client.aclose()

    async def list_groups(self, db: AsyncSession, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        client, headers, _cfg = await self._with_client(db, cfg)
        try:
            return await self._paginate_admin(client, headers, "/api/v1/admin/groups")
        finally:
            await client.aclose()

    async def get_account(self, db: AsyncSession, account_id: int) -> dict[str, Any]:
        if not account_id:
            return {}
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.get(f"/api/v1/admin/accounts/{int(account_id)}", headers=headers)
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        return data if isinstance(data, dict) else {}

    async def fetch_account_usage(
        self,
        db: AsyncSession,
        account_id: int,
        *,
        source: str = "active",
        force: bool = True,
    ) -> dict[str, Any]:
        if not account_id:
            return {}
        client, headers, _cfg = await self._with_client(db)
        params: dict[str, str] = {}
        if source:
            params["source"] = str(source)
        if force:
            params["force"] = "true"
        try:
            response = await client.get(
                f"/api/v1/admin/accounts/{int(account_id)}/usage",
                headers=headers,
                params=params or None,
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        return data if isinstance(data, dict) else {}

    async def set_account_schedulable(
        self,
        db: AsyncSession,
        account_id: int,
        schedulable: bool,
    ) -> dict[str, Any]:
        if not account_id:
            return {"account": {}, "patched": False, "patch": {}}
        wanted = bool(schedulable)
        patch = {"schedulable": wanted}
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.post(
                f"/api/v1/admin/accounts/{int(account_id)}/schedulable",
                headers=headers,
                json={"schedulable": wanted},
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        account = data if isinstance(data, dict) else {}
        if "schedulable" not in account:
            account["schedulable"] = wanted
        return {"account": account, "patched": True, "patch": patch}

    async def clear_account_rate_limit(self, db: AsyncSession, account_id: int) -> dict[str, Any]:
        if not account_id:
            return {}
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.post(
                f"/api/v1/admin/accounts/{int(account_id)}/clear-rate-limit",
                headers=headers,
            )
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        return data if isinstance(data, dict) else {}

    async def delete_accounts(self, db: AsyncSession, account_ids: list[int]) -> dict[str, Any]:
        ids: list[int] = []
        seen: set[int] = set()
        for raw in account_ids:
            account_id = _as_int(raw)
            if not account_id or account_id <= 0 or account_id in seen:
                continue
            seen.add(account_id)
            ids.append(account_id)
        if not ids:
            return {"deleted": [], "failed": []}
        deleted: list[int] = []
        failed: list[dict[str, Any]] = []
        client, headers, _cfg = await self._with_client(db)
        try:
            if len(ids) == 1:
                account_id = ids[0]
                response = await client.delete(f"/api/v1/admin/accounts/{account_id}", headers=headers)
                if response.status_code < 400:
                    deleted.append(account_id)
                else:
                    failed.append({"id": account_id, "status": response.status_code, "body": response.text[:240]})
            else:
                response = await client.post(
                    "/api/v1/admin/accounts/batch-delete",
                    headers=headers,
                    json={"account_ids": ids},
                )
                if response.status_code < 400:
                    deleted = list(ids)
                else:
                    failed = [{"id": account_id, "status": response.status_code} for account_id in ids]
        finally:
            await client.aclose()
        return {"deleted": deleted, "failed": failed}


sub2api_client = Sub2ApiClient()
