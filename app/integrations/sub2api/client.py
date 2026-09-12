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
        while page <= 1000:
            params = {"page": page, "page_size": 100, **(extra_params or {})}
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            payload = self._unwrap(response.json())
            items = self._account_items(payload)
            total = payload.get("total") if isinstance(payload, dict) else None
            if total is not None:
                try:
                    total = int(total)
                except (TypeError, ValueError):
                    raise RuntimeError("Sub2API pagination returned an invalid total") from None
                if total < 0:
                    raise RuntimeError("Sub2API pagination returned an invalid total")
            before = len(seen) + len(items_without_id)
            for item in items:
                item_id = self.remote_id(item)
                if item_id:
                    seen[item_id] = item
                else:
                    items_without_id.append(item)
            count = len(seen) + len(items_without_id)
            if total is not None and count >= total:
                return list(seen.values()) + items_without_id
            if not items:
                if total is not None and count < total:
                    raise RuntimeError("Sub2API account list is incomplete; existing bindings were preserved")
                return list(seen.values()) + items_without_id
            if count == before:
                raise RuntimeError("Sub2API pagination repeated a page; existing bindings were preserved")
            if total is None and len(items) < 100:
                return list(seen.values()) + items_without_id
            page += 1
        raise RuntimeError("Sub2API pagination limit reached; refusing a partial account list")

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
                try:
                    payload = response.json()
                    data = self._unwrap(payload)
                except Exception:
                    payload, data = {}, {}
                business_failed = (
                    isinstance(payload, dict) and (payload.get("success") is False or payload.get("error") or payload.get("code") not in (None, 0, "0", 200))
                ) or (isinstance(data, dict) and (data.get("success") is False or data.get("failed") or data.get("error")))
                if response.status_code in {200, 204} and not business_failed and (
                    response.status_code == 204
                    or isinstance(payload, dict) and payload.get("code") in (0, "0", 200)
                    or isinstance(data, dict) and data.get("success") is True
                ):
                    deleted.append(account_id)
                else:
                    failed.append({"id": account_id, "status": response.status_code, "body": response.text[:240]})
            else:
                response = await client.post(
                    "/api/v1/admin/accounts/batch-delete",
                    headers=headers,
                    json={"account_ids": ids},
                )
                if 200 <= response.status_code < 300:
                    try:
                        payload = response.json()
                        data = self._unwrap(payload)
                        if isinstance(payload, dict) and (payload.get("success") is False or payload.get("error")):
                            data = {}
                    except Exception:
                        data = {}
                    receipt = data if isinstance(data, dict) else {}
                    if receipt.get("success") is False or receipt.get("error"):
                        receipt = {}
                    failed_ids = {str(item.get("id")) for item in receipt.get("failed", []) if isinstance(item, dict)}
                    confirmed = {str(value) for value in receipt.get("deleted", [])}
                    deleted = [value for value in ids if str(value) in confirmed and str(value) not in failed_ids]
                    failed = [{"id": value, "status": response.status_code, "error": "delete_unconfirmed"}
                              for value in ids if value not in deleted]
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




    @staticmethod
    def _sync_failure(code: str, *, unknown: bool = False, supported: bool = True) -> dict[str, Any]:
        messages = {
            "sync_oauth_unsupported": "Sub2API 暂不支持所需的安全同步，请先升级服务端；本次未写入",
            "bridge_admin_auth_failed": "连接 Sub2API 的管理凭证被拒绝，请检查管理密钥；这不是账号 OAuth 失效",
            "bridge_unavailable": "无法连接 Sub2API，请检查地址、网络与连接配置；本次未写入",
            "remote_account_missing": "原远端账号不存在，已保留绑定等待核对，不会自动重建",
            "credential_version_conflict": "远端账号或同一操作的内容已变化，已停止覆盖，请核对最新状态",
            "candidate_validation_failed": "新授权的身份或 Codex 访问能力未验证通过，远端未写入凭据",
            "auth_error_unattributed": "远端错误缺少匹配的凭据版本证据，未写入凭据或清错",
            "auth_recovery_unsupported": "Sub2API 尚不支持经过验证的认证恢复，请先升级服务端",
        }
        return {
            "ok": False, "supported": supported, "error_code": code,
            "error": "远端同步结果未知，请核对操作回执，勿重复提交" if unknown else messages.get(code, "远端未接受安全凭据同步，请核对能力、身份与版本"),
            "credential_write": "unknown" if unknown else "not_attempted",
            "auth_recovery": "skipped", "token_cache_invalidation": "unknown" if unknown else "skipped",
            "partial": unknown, "state": "unknown" if unknown else "blocked",
        }

    def _unwrap_sync_error(self, response):
        try:
            payload = response.json()
            code = payload.get("reason") if isinstance(payload, dict) else None
            mapped = {"OAUTH_VALIDATION_FAILED": "candidate_validation_failed",
                      "AUTH_ERROR_UNATTRIBUTED": "auth_error_unattributed",
                      "AUTH_RECOVERY_UNSUPPORTED": "auth_recovery_unsupported"}.get(code)
            return self._sync_failure(mapped) if mapped else None
        except Exception:
            return None

    def _unwrap_sync_payload(self, payload: Any) -> Any:
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ValueError("invalid synchronization response")
        return self._unwrap(payload)

    def _parse_sync_receipt(self, data: Any, account_id: int, operation_id: str, expected_instance_id: str | None = None) -> dict[str, Any]:
        if expected_instance_id is not None and (not isinstance(data, dict) or data.get("instance_id") != expected_instance_id):
            return self._sync_failure("sync_oauth_identity_mismatch", unknown=True)
        if not isinstance(data, dict) or data.get("contract_version") != 1:
            return self._sync_failure("sync_oauth_bad_response", unknown=True)
        if str(data.get("remote_account_id") or "") != str(account_id) or data.get("operation_id") != operation_id:
            return self._sync_failure("sync_oauth_identity_mismatch", unknown=True)
        blockers = data.get("remaining_blockers")
        if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
            return self._sync_failure("sync_oauth_bad_response", unknown=True)
        if not isinstance(data.get("partial"), bool) or not isinstance(data.get("schedulable"), bool):
            return self._sync_failure("sync_oauth_bad_response", unknown=True)
        if data.get("auth_recovery") == "cleared":
            try:
                from app.integrations.sub2api.sync_state import _when
                if data.get("validation_scope") != "codex_identity_usage_catalog":
                    raise ValueError("validation missing")
                _when(data.get("validated_at"))
            except (ValueError, TypeError):
                return self._sync_failure("sync_oauth_bad_response", unknown=True)
        write_ok = data.get("credential_write") == "succeeded"
        steps_ok = (data.get("token_cache_invalidation") == "succeeded"
                    and data.get("auth_recovery") in {"skipped", "not_applicable", "cleared"}
                    and data.get("scheduler_refresh") == "succeeded" and data.get("state") == "completed"
                    and data.get("ok") is not False and data.get("success") is not False)
        complete = write_ok and steps_ok and not data["partial"] and not blockers
        # Persist only contract fields, never raw response bodies or credentials.
        return {
            "ok": complete, "supported": True, "contract_version": 1,
            "operation_id": operation_id, "remote_account_id": account_id,
            "instance_id": expected_instance_id,
            "scheduler_refresh": data.get("scheduler_refresh") if data.get("scheduler_refresh") in {"pending", "succeeded"} else "unknown",
            "credential_version": data.get("credential_version") if type(data.get("credential_version")) is int else None,
            "validation_scope": data.get("validation_scope") if data.get("validation_scope") == "codex_identity_usage_catalog" else None,
            "validated_at": data.get("validated_at") if isinstance(data.get("validated_at"), str) and len(data["validated_at"]) < 64 else None,
            "credential_write": "succeeded" if write_ok else "unknown",
            "auth_recovery": data.get("auth_recovery") if data.get("auth_recovery") in {"skipped", "not_applicable", "cleared", "failed", "conflict"} else "unknown",
            "token_cache_invalidation": data.get("token_cache_invalidation") if data.get("token_cache_invalidation") in {"succeeded", "failed", "pending", "unavailable"} else "unknown",
            "schedulable": data["schedulable"], "scheduling_assessment": "not_assessed",
            "remaining_blockers": [item for item in blockers if item in {
                "token_cache_pending", "final_state_unknown", "schedulable_off", "runtime_blocked",
                "account_error", "rate_limit", "temporary_pause", "overload", "account_expired",
            }],
            "partial": not complete, "state": data.get("state") if data.get("state") in {"pending", "completed", "needs_review"} else "partial",
            "error_code": None if complete else "sync_oauth_incomplete",
            "error": None if complete else "凭据同步未全部确认，保留已完成步骤",
        }

    async def sync_oauth_credentials(
        self, db: AsyncSession, account_id: int, *, credentials: dict[str, Any],
        expected_identity: dict[str, Any] | None = None,
        expected_updated_at: str | None = None, operation_id: str | None = None,
        recovery_mode: str = "credentials_only",
        expected_instance_id: str | None = None,
    ) -> dict[str, Any]:
        if not account_id or not operation_id or not expected_updated_at:
            return self._sync_failure("sync_precondition_missing")
        if recovery_mode not in {"credentials_only", "auth_only"}:
            return self._sync_failure("invalid_recovery_mode")
        from app.application.refresh_ownership import remote_accepts_access_token_only
        if await remote_accepts_access_token_only(db, account_id):
            credentials = {k: v for k, v in credentials.items() if k not in {"refresh_token", "id_token", "session_token"}}
        body = {
            "contract_version": 1, "operation_id": operation_id,
            "expected_updated_at": expected_updated_at, "recovery_mode": recovery_mode,
            "expected_identity": {
                "email": (expected_identity or {}).get("email"),
                "workspace_id": (expected_identity or {}).get("workspace_id"),
            },
            "credentials": {key: credentials[key] for key in (
                "access_token", "refresh_token", "id_token", "expires_at", "expired", "client_id",
            ) if key in credentials and credentials[key] not in (None, "")},
        }
        try:
            client, headers, _cfg = await self._with_client(db)
        except httpx.HTTPStatusError as exc:
            return self._sync_failure("bridge_admin_auth_failed" if exc.response.status_code in {401, 403} else "bridge_unavailable")
        except Exception:
            return self._sync_failure("bridge_unavailable")
        try:
            try:
                capability_response = await client.get("/api/v1/admin/integration/capabilities", headers=headers)
                if capability_response.status_code in {401, 403}:
                    return self._sync_failure("bridge_admin_auth_failed")
                if capability_response.status_code in {404, 405}:
                    return self._sync_failure("sync_oauth_unsupported", supported=False)
                capability_response.raise_for_status()
                capability = self._unwrap_sync_payload(capability_response.json())
                contract = capability.get("oauth_sync", {}) if isinstance(capability, dict) else {}
                if not isinstance(contract, dict) or contract.get("revision") not in {3, 4, 5} or any(
                    contract.get(key) is not True for key in ("available", "credential_cas", "operation_receipts", "atomic_receipts", "resumable_followups")
                ) or recovery_mode not in contract.get("recovery_modes", []):
                    return self._sync_failure("sync_oauth_unsupported", supported=False)
                if recovery_mode == "auth_only" and (contract.get("revision") not in {4, 5} or any(contract.get(k) is not True for k in ("auth_only", "candidate_validation", "versioned_auth_errors")) or contract.get("validation_scope") != "codex_identity_usage_catalog"):
                    return self._sync_failure("auth_recovery_unsupported", supported=False)
                from uuid import UUID
                instance_id = str(UUID(capability.get("instance_id", "")))
                if expected_instance_id is not None and instance_id != expected_instance_id:
                    return self._sync_failure("instance_mismatch")
                body["expected_instance_id"] = instance_id
            except Exception:
                return self._sync_failure("capability_check_failed")
            try:
                response = await client.post(f"/api/v1/admin/accounts/{int(account_id)}/sync-oauth-credentials", headers=headers, json=body)
                if response.status_code in {401, 403}:
                    return self._sync_failure("bridge_admin_auth_failed")
                if response.status_code == 404:
                    return self._sync_failure("remote_account_missing")
                if response.status_code in {400, 409}:
                    failure = self._unwrap_sync_error(response)
                    if failure is not None:
                        return failure
                if response.status_code == 409:
                    conflict = response.json()
                    if isinstance(conflict, dict) and conflict.get("reason") == "SYNC_INSTANCE_MISMATCH":
                        return self._sync_failure("instance_mismatch")
                    if isinstance(conflict, dict) and conflict.get("reason") in {
                        "CREDENTIAL_VERSION_CONFLICT", "IDEMPOTENCY_KEY_CONFLICT",
                    }:
                        return self._sync_failure("credential_version_conflict")
                    # In-progress/retry-backoff may follow a committed write.
                    raise ValueError("operation outcome requires receipt lookup")
                if 400 <= response.status_code < 500:
                    return self._sync_failure("sync_oauth_rejected")
                response.raise_for_status()
                result = self._parse_sync_receipt(self._unwrap_sync_payload(response.json()), account_id, operation_id, instance_id)
                if result.get("state") != "unknown":
                    return result
            except Exception:
                pass
            # POST may have committed. Query once; never retry the mutation or PUT.
            try:
                receipt_response = await client.get(
                    f"/api/v1/admin/accounts/{int(account_id)}/credential-sync-operations/{operation_id}", headers=headers,
                )
                receipt_response.raise_for_status()
                saved = self._unwrap_sync_payload(receipt_response.json())
                if isinstance(saved, dict) and saved.get("state") == "recorded" and saved.get("operation_id") == operation_id:
                    return self._parse_sync_receipt(saved.get("receipt"), account_id, operation_id, instance_id)
            except Exception:
                pass
            return self._sync_failure("sync_outcome_unknown", unknown=True)
        finally:
            await client.aclose()

    async def read_after_write(self, db: AsyncSession, account_id: int) -> dict[str, Any]:
        return await self.get_account(db, account_id)


sub2api_client = Sub2ApiClient()
