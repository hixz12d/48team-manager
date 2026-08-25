"""母号占用 / 操作上限 / 风险展示。不把本地上限当成上游订阅容量。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.utils.time_utils import get_now

RISK_RANK = {"critical": 3, "warning": 2, "unknown": 1, "normal": 0}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def present_occupancy(card: Dict[str, Any]) -> Dict[str, Any]:
    occupied = int(_as_int(card.get("current_members")) or 0)
    capacity = _as_int(card.get("max_members"))
    if capacity is not None and capacity <= 0:
        capacity = None

    live = card.get("live_members")
    upstream_occupied = None
    upstream_joined = None
    upstream_invited = None
    if isinstance(live, list) and not card.get("live_error"):
        upstream_joined = sum(
            1 for member in live if (member.get("status") or "joined") != "invited"
        )
        upstream_invited = sum(
            1 for member in live if (member.get("status") or "") == "invited"
        )
        upstream_occupied = upstream_joined + upstream_invited

    available = max(capacity - occupied, 0) if capacity is not None else None
    return {
        "occupied": occupied,
        "capacity": capacity,
        "capacity_known": capacity is not None,
        "available": available,
        "occupancy_text": f"{occupied}/{capacity}" if capacity is not None else f"{occupied}/未知",
        "capacity_label": "操作上限",
        "upstream_occupied": upstream_occupied,
        "upstream_joined": upstream_joined,
        "upstream_invited": upstream_invited,
        "over_capacity": bool(capacity is not None and occupied > capacity),
    }


def present_risks(
    card: Dict[str, Any],
    *,
    vacancy: Optional[Dict[str, Any]] = None,
    occupancy: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or get_now()
    occupancy = occupancy or present_occupancy(card)
    vacancy = vacancy if vacancy is not None else card.get("vacancy")
    items: List[Dict[str, str]] = []
    status = str(card.get("status") or "")

    if status == "banned":
        items.append({"key": "banned", "label": "管理号封禁", "level": "critical"})
    elif status == "error":
        items.append({"key": "error", "label": "同步异常", "level": "warning"})
    elif status == "expired":
        items.append({"key": "expired", "label": "凭证/订阅过期", "level": "critical"})

    expires_at = _as_datetime(card.get("expires_at"))
    if expires_at:
        if expires_at < now:
            items.append({"key": "subscription_expired", "label": "订阅已到期", "level": "critical"})
        elif expires_at - now <= timedelta(days=3):
            items.append({"key": "expiring_soon", "label": "三天内到期", "level": "warning"})

    if occupancy.get("over_capacity"):
        items.append({"key": "over_capacity", "label": "超过操作上限", "level": "warning"})

    if vacancy:
        if vacancy.get("has_billing_notice"):
            items.append({"key": "billing_notice", "label": "踢人带回执账单", "level": "warning"})
        elif vacancy.get("is_free") is False:
            items.append({"key": "seat_held", "label": "席位未释放", "level": "warning"})
        elif vacancy.get("safe_to_refill") is False:
            items.append({"key": "refill_unknown", "label": "勿自动补位", "level": "warning"})

    if not items:
        if status in {"active", "full"}:
            items.append({"key": "normal", "label": "正常", "level": "normal"})
        else:
            items.append({"key": "unknown", "label": "未知", "level": "unknown"})

    level = max(items, key=lambda item: RISK_RANK.get(item["level"], 0))["level"]
    label = next((item["label"] for item in items if item["level"] == level), items[0]["label"])
    return {"items": items, "level": level, "label": label}


def attach_operational_view(
    cards: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    current = now or get_now()
    for card in cards:
        occupancy = present_occupancy(card)
        risks = present_risks(card, occupancy=occupancy, now=current)
        card["occupancy"] = occupancy
        card["risks"] = risks
        card["risk_level"] = risks["level"]
        card["risk_label"] = risks["label"]
    return cards
