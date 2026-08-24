"""Sub2API Admin 导入。同机部署，不走代理。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings import settings_service

logger = logging.getLogger(__name__)


class Sub2ApiService:
    async def _config(self, db_session: AsyncSession) -> Dict[str, Any]:
        base_url = (await settings_service.get_setting(db_session, "sub2api_base_url", "")).strip().rstrip("/")
        api_key = (await settings_service.get_setting(db_session, "sub2api_api_key", "")).strip()
        group_raw = (await settings_service.get_setting(db_session, "sub2api_group_ids", "")).strip()
        group_ids: List[int] = []
        if group_raw:
            for part in group_raw.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    group_ids.append(int(part))
        return {"base_url": base_url, "api_key": api_key, "group_ids": group_ids}

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


sub2api_service = Sub2ApiService()
