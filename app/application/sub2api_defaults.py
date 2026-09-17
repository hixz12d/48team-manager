"""Creation defaults and read-only Sub2API selectors; allocation stays in Sub2API."""
from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.sub2api_proxy_catalog import _safe_text, sub2api_proxy_catalog
from app.integrations.sub2api.client import sub2api_client
from app.web.schemas.settings import Sub2ApiPushDefaults

SETTING_KEY = "sub2api_push_defaults"


async def load_defaults(db: AsyncSession) -> Sub2ApiPushDefaults:
    from app.application.settings import get_setting_value

    raw = await get_setting_value(db, SETTING_KEY)
    return Sub2ApiPushDefaults.model_validate_json(raw) if raw else Sub2ApiPushDefaults()


async def push_options(db: AsyncSession) -> dict:
    result = {"groups": [], "proxy_groups": [], "proxies": [], "errors": {}}
    cfg = await sub2api_client.load_config(db)
    for key, loader, label in (
        ("groups", sub2api_client.list_groups, "账号分组"),
        ("proxy_groups", sub2api_client.list_proxy_groups, "代理分组"),
        ("proxies", sub2api_client.list_proxies, "代理"),
    ):
        if not cfg.get("configured"):
            result["errors"][key] = "请先保存 Sub2API 连接设置"
            continue
        try:
            rows = await loader(db, cfg)
            if key == "proxies":
                result[key] = [item for row in rows if (item := sub2api_proxy_catalog.serialize(row)) and item["status"] == "active"]
            elif key == "groups":
                result[key] = [
                    {"id": row["id"], "name": _safe_text(row.get("name"), limit=100)}
                    for row in rows if row.get("platform") == "openai" and row.get("status") == "active" and not row.get("is_composite")
                ]
            else:
                result[key] = [
                    {"id": row["id"], "name": _safe_text(row.get("name"), limit=100),
                     "max_accounts_per_proxy": row.get("max_accounts_per_proxy"),
                     "proxy_count": len(row.get("proxy_ids") or []),
                     "available_proxy_count": len(row.get("available_proxy_ids") or [])}
                    for row in rows
                ]
        except Exception:
            result["errors"][key] = f"无法读取 Sub2API {label}，请检查连接和接口支持后重试"
    return result


async def validate_defaults(db: AsyncSession, defaults: Sub2ApiPushDefaults) -> list[int]:
    """Check selected resources without allocating or changing any proxy."""
    try:
        if defaults.group_ids:
            groups = await sub2api_client.list_groups(db)
            valid = {row["id"] for row in groups if row.get("platform") == "openai" and row.get("status") == "active" and not row.get("is_composite")}
            if not set(defaults.group_ids) <= valid:
                raise ValueError("所选账号分组已不可用，请在设置中重新选择 OpenAI 分组")
        if defaults.proxy_id:
            proxies = await sub2api_client.list_proxies(db)
            if not any(row.get("id") == defaults.proxy_id and row.get("status") == "active" for row in proxies):
                raise ValueError("所选固定代理已不可用，请在设置中重新选择")
        if defaults.proxy_group_id:
            groups = await sub2api_client.list_proxy_groups(db)
            group = next((row for row in groups if row.get("id") == defaults.proxy_group_id), None)
            if group is None:
                raise ValueError("所选代理分组已不存在，请在设置中重新选择")
            return group.get("proxy_ids") or []
    except ValueError:
        raise
    except Exception:
        raise ValueError("无法核对 Sub2API 推送默认值，请检查连接及代理分组接口后重试") from None
    return []


async def save_defaults(db: AsyncSession, defaults: Sub2ApiPushDefaults) -> None:
    from app.application.settings import upsert_setting

    await validate_defaults(db, defaults)
    await upsert_setting(db, SETTING_KEY, json.dumps(defaults.model_dump()), "Sub2API 新建账号默认值")
