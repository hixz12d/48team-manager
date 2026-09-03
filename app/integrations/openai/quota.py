"""Official quota client. Local token + workspace + proxy, never Sub2API."""

from __future__ import annotations

from datetime import datetime
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
        if not response.get("success"):
            return QuotaResult(
                success=False,
                error_code=http_error_code(response.get("status_code"), response.get("error_code")),
                error_message=str(response.get("error") or "official quota request failed")[:500],
                queried_at=now,
                raw=response.get("data") if isinstance(response.get("data"), dict) else {},
            )
        return parse_wham_usage(response.get("data") or {}, now=now)
