"""Official quota client. Local token + workspace + proxy, never Sub2API."""

from __future__ import annotations

from datetime import datetime

from app.core.time import utcnow
from app.domain.quota_health import LABELS, result_state, retry_after
from typing import Any

from app.domain.quota import (
    QuotaResult,
    QuotaTransport,
    http_error_code,
    parse_wham_usage,
)


class OpenAIQuotaClient:
    def __init__(self, transport: QuotaTransport | None = None):
        self._transport = transport

    async def fetch_quota(
        self,
        *,
        access_token: str,
        db_session: Any,
        workspace_id: str | None = None,
        proxy: str | None = None,
        identifier: str = "default",
        now: datetime | None = None,
    ) -> QuotaResult:
        del proxy
        token = str(access_token or "").strip()
        if not token:
            return QuotaResult(
                success=False,
                error_code="missing_token",
                error_message="这个号还没授权，无法读额度",
                queried_at=now,
            )
        account_id = str(workspace_id or "").strip() or None
        transport = self._transport
        if transport is None:
            from app.integrations.openai.chatgpt import chatgpt_client

            response = await chatgpt_client.get_wham_usage(
                token,
                db_session,
                account_id=account_id,
                identifier=identifier,
            )
        else:
            response = await transport.fetch(token, db_session, account_id, identifier)
        if not isinstance(response, dict):
            return QuotaResult(
                success=False,
                error_code="transport",
                error_message="quota transport returned non-dict",
                queried_at=now,
            )
        status = response.get("status_code")
        status = status if isinstance(status, int) and 100 <= status <= 599 else None
        if not response.get("success"):
            result = QuotaResult(
                success=False,
                error_code=http_error_code(response.get("status_code"), response.get("error_code")),
                http_status=status,
                request_count=int(response.get("attempts") or 1),
                retry_after_at=retry_after(response.get("retry_after"), now or utcnow()),
                error_source="proxy" if status == 407 else "official_quota",
                queried_at=now,
                raw=response.get("data") if isinstance(response.get("data"), dict) else {},
            )
            result.error_message = LABELS[result_state(result)][0]
            return result
        result = parse_wham_usage(response.get("data") or {}, now=now)
        result.http_status = status
        result.request_count = int(response.get("attempts") or 1)
        return result
