"""Create the new schema without touching a legacy production database file."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.persistence.database import init_db
from app.persistence.models import (  # noqa: F401
    Account,
    ExternalBinding,
    HmeAliasLease,
    Operation,
    OperationStep,
    OAuthSession,
    PhoneAttempt,
    PhonePool,
    ProxyProfile,
    QuotaSnapshot,
    SeatVacancyEvent,
    SystemSetting,
    Sub2ApiProxyBinding,
    Sub2ApiUsageSnapshot,
    Workspace,
    WorkspaceMembership,
    WorkspaceOfficialMemberSnapshot,
)

PROXY_PROFILE_COLUMNS = (
    ("health_state", "VARCHAR(20) DEFAULT 'unchecked' NOT NULL"),
    ("last_success_at", "DATETIME"),
    ("latency_ms", "INTEGER"),
    ("last_error", "TEXT"),
    ("name_source", "VARCHAR(20) DEFAULT 'auto' NOT NULL"),
)

SNAPSHOT_COLUMNS = (
    ("display_name", "VARCHAR(255)"),
    ("seat_type", "VARCHAR(40)"),
    ("added_at", "DATETIME"),
)

OPERATION_COLUMNS = (
    ("source", "VARCHAR(20) DEFAULT 'manual' NOT NULL"),
    ("archived_at", "DATETIME"),
    ("archive_reason", "VARCHAR(120)"),
)

WORKSPACE_COLUMNS = (
    ("official_name", "VARCHAR(255)"),
    ("custom_name", "VARCHAR(255)"),
    ("name_source", "VARCHAR(20) DEFAULT 'placeholder' NOT NULL"),
    ("official_name_synced_at", "DATETIME"),
    ("official_name_last_error", "VARCHAR(500)"),
    ("official_name_payload_source", "VARCHAR(80)"),
    ("occupied_seats", "INTEGER"),
    ("last_official_sync_state", "VARCHAR(20)"),
)

QUOTA_COLUMNS = (
    ("workspace_id", "INTEGER"),
)

BINDING_COLUMNS = (
    ("workspace_id", "INTEGER"),
)

ACCOUNT_COLUMNS = (
    ("proxy_source", "VARCHAR(20)"),
    ("sub2api_proxy_id", "INTEGER"),
    ("proxy_instance_key", "VARCHAR(64)"),
    ("mailbox_provider", "VARCHAR(20)"),
    ("hme_account_id", "VARCHAR(100)"),
    ("mailbox_binding_verified_at", "DATETIME"),
    ("mailbox_read_state", "VARCHAR(20) DEFAULT 'unknown' NOT NULL"),
    ("mailbox_checked_at", "DATETIME"),
    ("mailbox_method", "VARCHAR(20)"),
    ("auto_reauth_opt_in", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("credential_revision", "INTEGER DEFAULT 1 NOT NULL"),
)


async def _ensure_sqlite_columns(conn, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {
        str(row[1])
        for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    }
    for name, ddl in columns:
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def _index_names(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall()
    return {str(row[1]) for row in rows}


async def _rebuild_external_bindings(conn) -> None:
    existing = {
        str(row[1])
        for row in (await conn.execute(text("PRAGMA table_info(external_bindings)"))).fetchall()
    }
    if not existing:
        return
    indexes = await _index_names(conn, "external_bindings")
    has_workspace = "workspace_id" in existing
    has_old_local_unique = "uq_external_binding_local" in indexes
    has_new_local_unique = "uq_external_binding_local_workspace" in indexes
    if has_workspace and has_new_local_unique and not has_old_local_unique:
        return
    await conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS external_bindings_v2 (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            provider VARCHAR(40) NOT NULL,
            local_account_id INTEGER NOT NULL,
            remote_account_id VARCHAR(100) NOT NULL,
            workspace_id INTEGER,
            binding_state VARCHAR(20) DEFAULT 'pending' NOT NULL,
            verified_email VARCHAR(255),
            verified_official_account_id VARCHAR(100),
            verified_workspace_id VARCHAR(100),
            last_observed_at DATETIME,
            last_error TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_external_binding_remote UNIQUE (provider, remote_account_id),
            CONSTRAINT uq_external_binding_local_workspace UNIQUE (provider, local_account_id, workspace_id),
            FOREIGN KEY(local_account_id) REFERENCES accounts (id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
        )
        """
    ))
    workspace_select = "workspace_id" if has_workspace else "NULL AS workspace_id"
    await conn.execute(text(
        f"""
        INSERT INTO external_bindings_v2 (
            id, provider, local_account_id, remote_account_id, workspace_id,
            binding_state, verified_email, verified_official_account_id, verified_workspace_id,
            last_observed_at, last_error, created_at, updated_at
        )
        SELECT
            id, provider, local_account_id, remote_account_id, {workspace_select},
            binding_state, verified_email, verified_official_account_id, verified_workspace_id,
            last_observed_at, last_error, created_at, updated_at
        FROM external_bindings
        """
    ))
    await conn.execute(text("DROP TABLE external_bindings"))
    await conn.execute(text("ALTER TABLE external_bindings_v2 RENAME TO external_bindings"))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_external_binding_state ON external_bindings (provider, binding_state)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_external_binding_workspace ON external_bindings (provider, workspace_id)"
    ))


async def bootstrap_schema(engine: AsyncEngine) -> None:
    await init_db(engine)
    if not str(engine.url).startswith("sqlite"):
        return
    async with engine.begin() as conn:
        await _ensure_sqlite_columns(conn, "proxy_profiles", PROXY_PROFILE_COLUMNS)
        await _ensure_sqlite_columns(conn, "workspace_official_member_snapshots", SNAPSHOT_COLUMNS)
        await _ensure_sqlite_columns(conn, "operations", OPERATION_COLUMNS)
        await _ensure_sqlite_columns(conn, "workspaces", WORKSPACE_COLUMNS)
        await _ensure_sqlite_columns(conn, "quota_snapshots", QUOTA_COLUMNS)
        await _ensure_sqlite_columns(conn, "external_bindings", BINDING_COLUMNS)
        await _ensure_sqlite_columns(conn, "accounts", ACCOUNT_COLUMNS)
        await _rebuild_external_bindings(conn)
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_quota_snapshots_account_workspace_queried "
            "ON quota_snapshots (account_id, workspace_id, queried_at)"
        ))
