"""Read-only legacy SQLite importer.

Never imported by `app.main`. Mapping follows docs/refactor-current-dataflow.md §16.
Gmail / names / family prefixes are hints only and never become identity truth.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import (
    ensure_binding,
    ensure_membership,
    mark_duplicate_remote_bindings,
    upsert_child_account,
    upsert_mother_account,
    upsert_workspace,
)
from app.domain.identity import (
    LOCAL_PURPOSE_CHILD,
    LOCAL_PURPOSE_MOTHER,
    MEMBERSHIP_STATE_JOINED,
    OFFICIAL_ROLE_OWNER,
    OFFICIAL_ROLE_UNKNOWN,
)
from app.domain.identity.ids import normalize_email, workspace_official_id
from app.domain.identity.policy import mapping_membership_state


@dataclass
class MigrationReport:
    source: str
    dry_run: bool = True
    teams: int = 0
    accounts: int = 0
    workspaces: int = 0
    memberships: int = 0
    bindings: int = 0
    phones: int = 0
    hme: int = 0
    proxies: int = 0
    operations: int = 0
    mother_accounts: int = 0
    child_accounts: int = 0
    owner_memberships: int = 0
    mapping_memberships: int = 0
    binding_conflicts: int = 0
    skipped_user_ids: int = 0
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "dry_run": self.dry_run,
            "teams": self.teams,
            "accounts": self.accounts,
            "workspaces": self.workspaces,
            "memberships": self.memberships,
            "bindings": self.bindings,
            "phones": self.phones,
            "hme": self.hme,
            "proxies": self.proxies,
            "operations": self.operations,
            "mother_accounts": self.mother_accounts,
            "child_accounts": self.child_accounts,
            "owner_memberships": self.owner_memberships,
            "mapping_memberships": self.mapping_memberships,
            "binding_conflicts": self.binding_conflicts,
            "skipped_user_ids": self.skipped_user_ids,
            "conflicts": list(self.conflicts),
            "notes": list(self.notes),
        }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _table_exists(conn, table):
        return []
    return list(conn.execute(f"SELECT * FROM {table}"))


def inspect_legacy_db(path: Path, *, dry_run: bool = True) -> MigrationReport:
    """Read-only inspection. Does not write the new database."""
    report = MigrationReport(source=str(path), dry_run=dry_run)
    if not path.exists():
        report.notes.append("legacy database file not found")
        return report
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error:
        report.notes.append("legacy file is not a readable sqlite database")
        return report
    try:
        teams = _rows(conn, "teams")
        children = _rows(conn, "child_accounts")
        mappings = _rows(conn, "team_email_mappings")
        report.teams = len(teams)
        report.accounts = len(teams) + len(children)
        report.notes.append("read-only inspect; names and Gmail are hints only")
        for team in teams:
            account_id = _get(team, "account_id")
            if account_id and workspace_official_id(account_id) is None:
                report.skipped_user_ids += 1
                report.conflicts.append(
                    f"Team {team['id']} account_id={account_id} is not a Workspace UUID"
                )
        if mappings:
            report.notes.append(f"{len(mappings)} team_email_mappings would become memberships")
        if children:
            report.notes.append(f"{len(children)} child_accounts would become local accounts")
    finally:
        conn.close()
    return report


def _get(row: sqlite3.Row, key: str, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


async def import_legacy_identity(path: Path, db: AsyncSession) -> MigrationReport:
    """Project legacy tables into the new identity schema. Does not modify the source file."""
    report = inspect_legacy_db(path, dry_run=False)
    if not path.exists() or any("not a readable sqlite" in note for note in report.notes):
        return report
    conn = _connect_readonly(path)
    try:
        teams = _rows(conn, "teams")
        children = _rows(conn, "child_accounts")
        mappings = _rows(conn, "team_email_mappings")
    finally:
        conn.close()

    workspaces_by_team: dict[int, Any] = {}
    accounts_by_email: dict[str, Any] = {}

    for team in teams:
        email = normalize_email(_get(team, "email"))
        if not email:
            continue
        account, created = await upsert_mother_account(
            db,
            email=email,
            source_team_id=int(team["id"]),
            operational_state="active" if (_get(team, "status") or "active") == "active" else _get(team, "status"),
            proxy=_get(team, "proxy"),
            access_token_encrypted=_get(team, "access_token_encrypted"),
            refresh_token_encrypted=_get(team, "refresh_token_encrypted"),
            session_token_encrypted=_get(team, "session_token_encrypted"),
            id_token_encrypted=_get(team, "id_token_encrypted"),
            client_id=_get(team, "client_id"),
        )
        accounts_by_email[email] = account
        if created:
            report.mother_accounts += 1

        official_id = workspace_official_id(_get(team, "account_id"))

        workspace, ws_created = await upsert_workspace(
            db,
            source_team_id=int(team["id"]),
            official_workspace_id=official_id,
            name=_get(team, "team_name"),
            subscription_plan=_get(team, "subscription_plan") or _get(team, "plan_type"),
            owner_account_id=account.id,
            status=_get(team, "status") or "active",
            seat_limit=_get(team, "max_members"),
            last_official_sync_at=_get(team, "last_sync"),
        )
        workspaces_by_team[int(team["id"])] = workspace
        if ws_created:
            report.workspaces += 1

        if await ensure_membership(
            db,
            workspace_id=workspace.id,
            account_id=account.id,
            official_role=OFFICIAL_ROLE_OWNER,
            membership_state=MEMBERSHIP_STATE_JOINED,
            local_purpose=LOCAL_PURPOSE_MOTHER,
            joined_at=_get(team, "created_at"),
        ):
            report.owner_memberships += 1

        if await ensure_binding(db, account=account, remote_account_id=_get(team, "sub2api_account_id")):
            report.bindings += 1

    for child in children:
        email = normalize_email(_get(child, "email"))
        if not email:
            continue
        account, created = await upsert_child_account(
            db,
            email=email,
            status=_get(child, "status"),
            source_child_account_id=int(child["id"]),
            proxy=_get(child, "proxy"),
            access_token_encrypted=_get(child, "access_token_encrypted"),
            refresh_token_encrypted=_get(child, "refresh_token_encrypted"),
            session_token_encrypted=_get(child, "session_token_encrypted"),
            id_token_encrypted=_get(child, "id_token_encrypted"),
            client_id=_get(child, "client_id"),
            next_eligible_at=_get(child, "next_eligible_at"),
        )
        accounts_by_email[email] = account
        if created:
            report.child_accounts += 1
        if await ensure_binding(db, account=account, remote_account_id=_get(child, "sub2api_account_id")):
            report.bindings += 1

    for mapping in mappings:
        email = normalize_email(_get(mapping, "email"))
        if not email:
            continue
        workspace = workspaces_by_team.get(int(mapping["team_id"]))
        if workspace is None:
            continue
        account = accounts_by_email.get(email)
        if account is None:
            account, created = await upsert_child_account(db, email=email, status="unused")
            accounts_by_email[email] = account
            if created:
                report.child_accounts += 1
        if account.local_purpose == LOCAL_PURPOSE_MOTHER:
            continue
        if await ensure_membership(
            db,
            workspace_id=workspace.id,
            account_id=account.id,
            official_role=OFFICIAL_ROLE_UNKNOWN,
            membership_state=mapping_membership_state(_get(mapping, "status")),
            local_purpose=LOCAL_PURPOSE_CHILD,
            joined_at=_get(mapping, "joined_at"),
            removed_at=_get(mapping, "kicked_at"),
            source_mapping_id=int(mapping["id"]),
        ):
            report.mapping_memberships += 1

    await db.flush()
    report.binding_conflicts = await mark_duplicate_remote_bindings(db)
    await db.commit()
    report.accounts = report.mother_accounts + report.child_accounts
    report.memberships = report.owner_memberships + report.mapping_memberships
    report.notes.append("legacy source file was opened read-only")
    return report
