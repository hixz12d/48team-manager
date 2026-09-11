"""Best-effort official Workspace name refresh. Never fails member sync."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.tokens import decrypt_secret
from app.core.jwt import jwt_parser
from app.core.time import isoformat, utcnow
from app.domain.workspaces.metadata import resolve_official_title
from app.domain.workspaces.names import apply_official_name, resolve_display_name
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.models.identity import Account, Workspace


class WorkspaceMetadataResolver:
    def __init__(self, client=None):
        self.client = client or chatgpt_client

    def _jwt_orgs(self, *tokens: str | None) -> list[dict[str, Any]]:
        orgs: list[dict[str, Any]] = []
        for token in tokens:
            if not token:
                continue
            orgs.extend(jwt_parser.extract_organizations(token))
        return orgs

    async def collect_payloads(
        self,
        db: AsyncSession,
        workspace: Workspace,
        owner: Account | None,
        extra: list[tuple[str, Any]] | None = None,
    ) -> list[tuple[str, Any]]:
        payloads: list[tuple[str, Any]] = list(extra or [])
        if owner is None:
            return payloads
        access = decrypt_secret(owner.access_token_encrypted)
        id_token = decrypt_secret(getattr(owner, "id_token_encrypted", None))
        if access and workspace.official_workspace_id:
            try:
                async with asyncio.timeout(10):
                    context = await self.client.get_account_context(
                        access,
                        db,
                        account_id=str(workspace.official_workspace_id),
                        identifier=owner.email,
                    )
            except Exception as exc:  # noqa: BLE001
                payloads.append(("account_context_error", {"error": str(exc)[:200]}))
            else:
                if context.get("success"):
                    payloads.append(
                        (
                            f"account_context:{context.get('endpoint') or 'accounts/check'}",
                            context.get("data"),
                        )
                    )
                else:
                    payloads.append(
                        (
                            "account_context_miss",
                            {
                                "error": context.get("error"),
                                "error_code": context.get("error_code"),
                                "status_code": context.get("status_code"),
                                "endpoint": context.get("endpoint"),
                            },
                        )
                    )
        # Token claims may contain a name from before the latest official rename.
        jwt_orgs = self._jwt_orgs(access, id_token)
        if jwt_orgs:
            payloads.append(("jwt_organizations", jwt_orgs))
        return payloads

    def apply_resolved(
        self,
        workspace: Workspace,
        resolved: dict[str, Any],
        *,
        owner_email: str | None,
        stamp=None,
    ) -> dict[str, Any]:
        stamp = stamp or utcnow()
        title = resolved.get("title")
        error = resolved.get("error") if not title else None
        apply_official_name(
            workspace,
            title,
            owner_email=owner_email,
            synced_at=stamp if title else getattr(workspace, "official_name_synced_at", None),
            payload_source=resolved.get("payload_source"),
            last_error=error,
        )
        display = resolve_display_name(workspace, owner_email=owner_email)
        return {
            "ok": True,
            "found": bool(title),
            "official_name": display.get("official_name"),
            "display_name": display.get("display_name"),
            "name_source": display.get("name_source"),
            "custom_name": display.get("custom_name"),
            "official_name_payload_source": display.get("official_name_payload_source"),
            "official_name_last_error": display.get("official_name_last_error"),
            "official_name_synced_at": isoformat(getattr(workspace, "official_name_synced_at", None)),
            "error": error,
        }

    async def refresh(
        self,
        db: AsyncSession,
        workspace: Workspace,
        owner: Account | None,
        extra: list[tuple[str, Any]] | None = None,
        *,
        persist: bool = False,
    ) -> dict[str, Any]:
        owner_email = owner.email if owner else None
        payloads = await self.collect_payloads(db, workspace, owner, extra=extra)
        resolved = resolve_official_title(
            payloads,
            workspace.official_workspace_id,
            owner_email=owner_email,
        )
        result = self.apply_resolved(workspace, resolved, owner_email=owner_email)
        workspace.updated_at = utcnow()
        if persist:
            await db.commit()
        return result


workspace_metadata_resolver = WorkspaceMetadataResolver()
