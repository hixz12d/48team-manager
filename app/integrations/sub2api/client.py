"""Sub2API Admin HTTP. Binding and runtime only; never compose or guess identity from names."""

from __future__ import annotations

import asyncio
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
        timeout = httpx.Timeout(30.0, connect=5.0, read=30.0, write=15.0, pool=5.0)
        client = httpx.AsyncClient(base_url=cfg["base_url"], timeout=timeout)
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

    async def list_proxies(
        self,
        db: AsyncSession,
        cfg: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        client, headers, _cfg = await self._with_client(db, cfg)
        try:
            return await self._paginate_admin(client, headers, "/api/v1/admin/proxies")
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
        # Do not treat the request target as verified remote state when the field is absent.
        verified = "schedulable" in account
        return {
            "account": account,
            "patched": True,
            "patch": patch,
            "schedulable_verified": verified,
            "schedulable": account.get("schedulable") if verified else None,
        }

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

    async def find_account_by_email(self, db: AsyncSession, email: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
        target = normalize_email(email)
        if not target:
            return None
        accounts = await self.list_status_accounts(db, cfg)
        for item in accounts:
            if self.account_email(item) == target:
                return item
        return None

    async def create_account(self, db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.post("/api/v1/admin/accounts", headers=headers, json=payload)
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        return data if isinstance(data, dict) else {}

    async def update_account(self, db: AsyncSession, account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not account_id:
            return {}
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.put(f"/api/v1/admin/accounts/{int(account_id)}", headers=headers, json=payload)
            response.raise_for_status()
            data = self._unwrap(response.json())
        finally:
            await client.aclose()
        return data if isinstance(data, dict) else {}

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> Any:
        """Run a safe Admin API request with bounded transient retries."""
        last_error: Exception | None = None
        for attempt in range(max(0, retries) + 1):
            try:
                response = await client.request(method, path, headers=headers, params=params, json=json)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        await asyncio.sleep(0.25 * (attempt + 1))
                        continue
                response.raise_for_status()
                return self._unwrap(response.json())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= retries:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise RuntimeError("Sub2API request failed")

    async def fetch_billing_windows(
        self,
        db: AsyncSession,
        account_ids: list[int],
        *,
        force_usage: bool = False,
    ) -> dict[str, Any]:
        """Fetch 5h, today, and natural 7-day windows using one authenticated client."""
        ids = sorted({int(value) for value in account_ids if _as_int(value) and int(value) > 0})
        result: dict[str, Any] = {
            "five_hour": {},
            "today": {},
            "seven_day": {},
            "errors": {},
        }
        if not ids:
            return result

        client, headers, _cfg = await self._with_client(db)
        try:
            current_call = self._request_json(
                client,
                headers,
                "POST",
                "/api/v1/admin/accounts/usage/batch",
                json={"account_ids": ids, "force": bool(force_usage)},
            )
            today_call = self._request_json(
                client,
                headers,
                "POST",
                "/api/v1/admin/accounts/today-stats/batch",
                json={"account_ids": ids},
            )
            current, today = await asyncio.gather(current_call, today_call, return_exceptions=True)

            if isinstance(current, Exception):
                for account_id in ids:
                    result["errors"].setdefault(str(account_id), {})["five_hour"] = str(current)
            elif isinstance(current, dict):
                usage_map = current.get("usage") if isinstance(current.get("usage"), dict) else {}
                error_map = current.get("errors") if isinstance(current.get("errors"), dict) else {}
                for account_id in ids:
                    key = str(account_id)
                    usage = usage_map.get(key, usage_map.get(account_id))
                    five = usage.get("five_hour") if isinstance(usage, dict) else None
                    if isinstance(five, dict) and isinstance(five.get("window_stats"), dict):
                        result["five_hour"][key] = five
                    elif error_map.get(key) or error_map.get(account_id):
                        result["errors"].setdefault(key, {})["five_hour"] = str(
                            error_map.get(key) or error_map.get(account_id)
                        )

            if isinstance(today, Exception):
                for account_id in ids:
                    result["errors"].setdefault(str(account_id), {})["today"] = str(today)
            elif isinstance(today, dict):
                stats_map = today.get("stats") if isinstance(today.get("stats"), dict) else {}
                for account_id in ids:
                    key = str(account_id)
                    stats = stats_map.get(key, stats_map.get(account_id))
                    if isinstance(stats, dict):
                        result["today"][key] = stats

            semaphore = asyncio.Semaphore(6)

            async def fetch_seven(account_id: int) -> tuple[int, Any]:
                async with semaphore:
                    try:
                        payload = await self._request_json(
                            client,
                            headers,
                            "GET",
                            f"/api/v1/admin/accounts/{account_id}/stats",
                            params={"days": "7"},
                        )
                        return account_id, payload
                    except Exception as exc:
                        return account_id, exc

            seven_results = await asyncio.gather(*(fetch_seven(account_id) for account_id in ids))
            for account_id, payload in seven_results:
                key = str(account_id)
                if isinstance(payload, Exception):
                    result["errors"].setdefault(key, {})["seven_day"] = str(payload)
                    continue
                summary = payload.get("summary") if isinstance(payload, dict) else None
                if isinstance(summary, dict):
                    result["seven_day"][key] = summary
            return result
        finally:
            await client.aclose()

    async def get_proxy(self, db: AsyncSession, proxy_id: int) -> dict[str, Any]:
        client, headers, _cfg = await self._with_client(db)
        try:
            data = await self._request_json(
                client, headers, "GET", f"/api/v1/admin/proxies/{int(proxy_id)}"
            )
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()

    async def create_proxy(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        client, headers, _cfg = await self._with_client(db)
        request_headers = dict(headers)
        if idempotency_key:
            request_headers["Idempotency-Key"] = str(idempotency_key)[:120]
        try:
            data = await self._request_json(
                client, request_headers, "POST", "/api/v1/admin/proxies", json=payload, retries=1
            )
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()

    async def update_proxy(
        self, db: AsyncSession, proxy_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        client, headers, _cfg = await self._with_client(db)
        try:
            data = await self._request_json(
                client,
                headers,
                "PUT",
                f"/api/v1/admin/proxies/{int(proxy_id)}",
                json=payload,
                retries=1,
            )
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()

    async def test_proxy(self, db: AsyncSession, proxy_id: int) -> dict[str, Any]:
        client, headers, _cfg = await self._with_client(db)
        try:
            data = await self._request_json(
                client,
                headers,
                "POST",
                f"/api/v1/admin/proxies/{int(proxy_id)}/test",
                retries=1,
            )
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()

    async def integration_capabilities(self, db: AsyncSession) -> dict[str, Any]:
        """Detect the Sub2API contracts Team48 actively consumes."""
        fallback = {
            "schema_version": None,
            "detection": "legacy_fallback",
            "usage": {
                "batch_current": True,
                "batch_today": True,
                "batch_exact_windows": False,
            },
            "proxies": {"catalog": True, "test": True},
        }
        config = await self.load_config(db)
        if not config.get("configured"):
            return {**fallback, "detection": "unconfigured"}
        client, headers, _cfg = await self._with_client(db)
        try:
            try:
                data = await self._request_json(
                    client,
                    headers,
                    "GET",
                    "/api/v1/admin/integration/capabilities",
                    retries=0,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return fallback
                raise
            if not isinstance(data, dict):
                return fallback
            remote = dict(data)
            remote.pop("account_templates", None)
            return {**fallback, **remote, "detection": "remote"}
        finally:
            await client.aclose()




    async def sync_oauth_credentials(
        self,
        db: AsyncSession,
        account_id: int,
        *,
        credentials: dict[str, Any],
        expected_identity: dict[str, Any] | None = None,
        expected_updated_at: str | None = None,
        operation_id: str | None = None,
        recovery_mode: str = "credentials_only",
    ) -> dict[str, Any]:
        """Call the narrow Sub2API credential sync contract when available.

        Returns a structured result. 404/405 means capability missing — callers
        must not fall back to broad clear-error recovery.
        """
        if not account_id:
            return {
                "ok": False,
                "supported": False,
                "error_code": "missing_remote_id",
                "error": "missing remote account id",
            }
        mode = str(recovery_mode or "credentials_only").strip() or "credentials_only"
        if mode not in {"credentials_only", "auth_only"}:
            return {
                "ok": False,
                "supported": True,
                "error_code": "invalid_recovery_mode",
                "error": f"unsupported recovery_mode={mode}",
            }
        body: dict[str, Any] = {
            "contract_version": 1,
            "recovery_mode": mode,
            "credentials": {
                key: credentials[key]
                for key in ("access_token", "refresh_token", "id_token", "expires_at", "expired")
                if key in credentials and credentials.get(key) not in (None, "")
            },
        }
        if operation_id:
            body["operation_id"] = str(operation_id)
        if expected_updated_at:
            body["expected_updated_at"] = str(expected_updated_at)
        if expected_identity:
            body["expected_identity"] = {
                key: expected_identity[key]
                for key in ("email", "workspace_id", "official_account_id")
                if expected_identity.get(key) not in (None, "")
            }
        client, headers, _cfg = await self._with_client(db)
        try:
            response = await client.post(
                f"/api/v1/admin/accounts/{int(account_id)}/sync-oauth-credentials",
                headers=headers,
                json=body,
            )
            if response.status_code in {404, 405}:
                return {
                    "ok": False,
                    "supported": False,
                    "error_code": "sync_oauth_unsupported",
                    "error": "远端未提供 sync-oauth-credentials 窄接口",
                    "upstream_status": response.status_code,
                }
            if response.status_code in {401, 403}:
                detail = (response.text or "")[:240]
                return {
                    "ok": False,
                    "supported": True,
                    "error_code": "admin_auth_failed",
                    "error": "Team 调用 Sub2API Admin 接口鉴权失败，不是子号 OAuth 掉授权",
                    "upstream_status": response.status_code,
                    "detail": detail,
                }
            if response.status_code >= 400:
                detail = (response.text or "")[:240]
                return {
                    "ok": False,
                    "supported": True,
                    "error_code": "sync_oauth_failed",
                    "error": detail or f"HTTP {response.status_code}",
                    "upstream_status": response.status_code,
                }
            try:
                payload = response.json()
            except Exception:
                payload = {}
            data = self._unwrap(payload) if isinstance(payload, dict) else payload
            if not isinstance(data, dict):
                return {
                    "ok": False,
                    "supported": True,
                    "error_code": "sync_oauth_bad_response",
                    "error": "sync-oauth-credentials 返回形状无效",
                }
            if int(data.get("contract_version") or 0) != 1:
                return {
                    "ok": False,
                    "supported": True,
                    "error_code": "sync_oauth_contract_mismatch",
                    "error": f"unexpected contract_version={data.get('contract_version')!r}",
                    "raw_keys": sorted(str(k) for k in data.keys())[:20],
                }
            return {
                "ok": True,
                "supported": True,
                "contract_version": 1,
                "operation_id": data.get("operation_id") or operation_id,
                "remote_account_id": data.get("remote_account_id") or account_id,
                "credential_write": data.get("credential_write") or "unknown",
                "token_cache_invalidation": data.get("token_cache_invalidation") or "unknown",
                "auth_recovery": data.get("auth_recovery") or "skipped",
                "schedulable": data.get("schedulable"),
                "scheduling_assessment": data.get("scheduling_assessment") or "unknown",
                "remaining_blockers": list(data.get("remaining_blockers") or []),
                "partial": bool(data.get("partial")),
                "raw": data,
            }
        finally:
            await client.aclose()

    async def read_after_write(self, db: AsyncSession, account_id: int) -> dict[str, Any]:
        return await self.get_account(db, account_id)


sub2api_client = Sub2ApiClient()
