"""Legacy SQLite importer skeleton.

PHASE B only records the contract. Identity mapping lands in PHASE C.
This module is never imported by `app.main`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
            "conflicts": list(self.conflicts),
            "notes": list(self.notes),
        }


def inspect_legacy_db(path: Path, *, dry_run: bool = True) -> MigrationReport:
    """Read-only inspection. Does not write the new database."""
    report = MigrationReport(source=str(path), dry_run=dry_run)
    if not path.exists():
        report.notes.append("legacy database file not found")
        return report
    report.notes.append("importer skeleton only; mapping runs in a later phase")
    return report
