"""Phase 9：兑换码 / 质保 / 续期 / 福利 / 公告已下线。

页面和 API 返回 410，数据表与服务代码先留着，最后再删。
"""
import re

from fastapi.responses import JSONResponse

LEGACY_SALES_PATH = re.compile(
    r"^/(?:redeem|warranty)(?:/|$)"
    r"|^/admin/(?:welfare|codes|records|announcement|renewal-requests)(?:/|$)"
    r"|^/admin/teams/[^/]+/warranty-seat$"
    r"|^/admin/teams/batch-transfer-pool$"
    r"|^/admin/settings/warranty(?:-auto-kick)?$"
)

GONE_PAYLOAD = {
    "success": False,
    "error": "legacy_feature_removed",
    "detail": "兑换码 / 质保 / 续期 / 福利 / 公告已下线，数据表保留。",
}


def is_legacy_sales_path(path: str) -> bool:
    return bool(LEGACY_SALES_PATH.match(path or ""))


def legacy_gone_response() -> JSONResponse:
    return JSONResponse(status_code=410, content=GONE_PAYLOAD)
