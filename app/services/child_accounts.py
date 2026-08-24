"""子号资产库。踢人只改状态，删除才是单独动作。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChildAccount, SeatEvent, Team, TeamEmailMapping
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

CHILD_STATUS_UNUSED = "unused"
CHILD_STATUS_INVITED = "invited"
CHILD_STATUS_ACTIVE = "active"
CHILD_STATUS_STANDBY = "standby"
CHILD_STATUS_DISABLED = "disabled"
CHILD_STATUS_DELETED = "deleted"

ACTIVE_CHILD_STATUSES = (CHILD_STATUS_INVITED, CHILD_STATUS_ACTIVE)
REUSABLE_CHILD_STATUSES = (CHILD_STATUS_UNUSED, CHILD_STATUS_STANDBY, CHILD_STATUS_DISABLED)


def normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


class ChildAccountService:
    def encrypt_secret(self, value: Optional[str]) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        return encryption_service.encrypt_token(text)

    def decrypt_secret(self, value: Optional[str]) -> str:
        if not value:
            return ""
        try:
            return encryption_service.decrypt_token(value)
        except Exception as exc:
            logger.warning("解密子号密钥失败: %s", exc)
            return ""

    def serialize(self, child: ChildAccount, *, include_secrets: bool = False) -> Dict[str, Any]:
        now = get_now()
        cycle_days = int(child.cycle_days or 7)
        due_at = None
        remaining_days = None
        if child.joined_at and child.status in ACTIVE_CHILD_STATUSES:
            due_at = child.joined_at + timedelta(days=cycle_days)
            remaining_days = max(0, (due_at.date() - now.date()).days)

        data = {
            "id": child.id,
            "email": child.email,
            "status": child.status,
            "current_team_id": child.current_team_id,
            "last_team_id": child.last_team_id,
            "joined_at": child.joined_at.isoformat() if child.joined_at else None,
            "kicked_at": child.kicked_at.isoformat() if child.kicked_at else None,
            "due_at": due_at.isoformat() if due_at else None,
            "remaining_days": remaining_days,
            "cycle_days": cycle_days,
            "proxy": child.proxy or "",
            "phone": child.phone or "",
            "sms_url": child.sms_url or "",
            "mail_raw": child.mail_raw or "",
            "account_id": child.account_id or "",
            "client_id": child.client_id or "",
            "sub2api_account_id": child.sub2api_account_id,
            "last_error": child.last_error or "",
            "created_at": child.created_at.isoformat() if child.created_at else None,
            "updated_at": child.updated_at.isoformat() if child.updated_at else None,
        }
        if include_secrets:
            data["password"] = self.decrypt_secret(child.password_encrypted)
            data["access_token"] = self.decrypt_secret(child.access_token_encrypted)
            data["refresh_token"] = self.decrypt_secret(child.refresh_token_encrypted)
            data["session_token"] = self.decrypt_secret(child.session_token_encrypted)
            data["id_token"] = self.decrypt_secret(child.id_token_encrypted)
        return data

    async def get_by_email(self, db_session: AsyncSession, email: str) -> Optional[ChildAccount]:
        normalized = normalize_email(email)
        if not normalized:
            return None
        result = await db_session.execute(select(ChildAccount).where(ChildAccount.email == normalized))
        return result.scalar_one_or_none()

    async def get_by_id(self, db_session: AsyncSession, child_id: int) -> Optional[ChildAccount]:
        return await db_session.get(ChildAccount, child_id)

    async def list_accounts(
        self,
        db_session: AsyncSession,
        *,
        status: Optional[str] = None,
        team_id: Optional[int] = None,
        search: Optional[str] = None,
    ) -> List[ChildAccount]:
        stmt = select(ChildAccount).where(ChildAccount.status != CHILD_STATUS_DELETED)
        if status:
            stmt = stmt.where(ChildAccount.status == status)
        if team_id:
            stmt = stmt.where(ChildAccount.current_team_id == team_id)
        if search:
            like = f"%{search.strip()}%"
            stmt = stmt.where(or_(ChildAccount.email.ilike(like), ChildAccount.phone.ilike(like)))
        stmt = stmt.order_by(ChildAccount.updated_at.desc(), ChildAccount.id.desc())
        result = await db_session.execute(stmt)
        return list(result.scalars().all())

    async def list_due_accounts(
        self,
        db_session: AsyncSession,
        *,
        team_id: Optional[int] = None,
    ) -> List[ChildAccount]:
        now = get_now()
        stmt = select(ChildAccount).where(ChildAccount.status == CHILD_STATUS_ACTIVE)
        if team_id:
            stmt = stmt.where(ChildAccount.current_team_id == team_id)
        result = await db_session.execute(stmt)
        due: List[ChildAccount] = []
        for child in result.scalars().all():
            if not child.joined_at:
                continue
            cycle_days = int(child.cycle_days or 7)
            if child.joined_at + timedelta(days=cycle_days) <= now:
                due.append(child)
        due.sort(key=lambda item: item.joined_at or now)
        return due

    async def upsert_from_input(
        self,
        db_session: AsyncSession,
        *,
        email: str,
        password: str = "",
        mail_raw: str = "",
        phone: str = "",
        sms_url: str = "",
        proxy: str = "",
        cycle_days: Optional[int] = None,
    ) -> ChildAccount:
        normalized = normalize_email(email)
        if not normalized or "@" not in normalized:
            raise ValueError("邮箱格式不正确")

        child = await self.get_by_email(db_session, normalized)
        if not child:
            child = ChildAccount(email=normalized, status=CHILD_STATUS_UNUSED)
            db_session.add(child)

        if password:
            child.password_encrypted = self.encrypt_secret(password)
        if mail_raw:
            child.mail_raw = mail_raw.strip()
        if phone:
            child.phone = phone.strip()
        if sms_url:
            child.sms_url = sms_url.strip()
        if proxy:
            from app.utils.proxy import normalize_proxy_url

            child.proxy = normalize_proxy_url(proxy)
        if cycle_days is not None:
            child.cycle_days = max(1, int(cycle_days))
        if child.status == CHILD_STATUS_DELETED:
            child.status = CHILD_STATUS_UNUSED
        child.updated_at = get_now()
        await db_session.flush()
        return child

    async def save_tokens(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        tokens: Dict[str, Any],
    ) -> None:
        if tokens.get("access_token"):
            child.access_token_encrypted = self.encrypt_secret(tokens["access_token"])
        if tokens.get("refresh_token"):
            child.refresh_token_encrypted = self.encrypt_secret(tokens["refresh_token"])
        if tokens.get("session_token"):
            child.session_token_encrypted = self.encrypt_secret(tokens["session_token"])
        if tokens.get("id_token"):
            child.id_token_encrypted = self.encrypt_secret(tokens["id_token"])
        if tokens.get("client_id"):
            child.client_id = str(tokens["client_id"]).strip()
        if tokens.get("account_id"):
            child.account_id = str(tokens["account_id"]).strip()
        child.updated_at = get_now()
        await db_session.flush()

    async def mark_invited(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Team,
    ) -> None:
        child.status = CHILD_STATUS_INVITED
        child.current_team_id = team.id
        child.cycle_days = int(getattr(team, "seat_cycle_days", None) or child.cycle_days or 7)
        child.last_error = None
        child.updated_at = get_now()
        await db_session.flush()

    async def mark_active(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Team,
        *,
        mapping: Optional[TeamEmailMapping] = None,
    ) -> None:
        now = get_now()
        child.status = CHILD_STATUS_ACTIVE
        child.current_team_id = team.id
        child.joined_at = now
        child.kicked_at = None
        child.cycle_days = int(getattr(team, "seat_cycle_days", None) or child.cycle_days or 7)
        child.last_error = None
        child.updated_at = now
        if mapping:
            mapping.child_account_id = child.id
            mapping.joined_at = now
            mapping.kicked_at = None
            mapping.cycle_days = child.cycle_days
        await db_session.flush()

    async def mark_standby(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        *,
        mapping: Optional[TeamEmailMapping] = None,
        error: Optional[str] = None,
    ) -> None:
        now = get_now()
        if child.current_team_id:
            child.last_team_id = child.current_team_id
        child.status = CHILD_STATUS_STANDBY
        child.current_team_id = None
        child.kicked_at = now
        child.last_error = error
        child.updated_at = now
        if mapping:
            mapping.kicked_at = now
            mapping.child_account_id = child.id
        await db_session.flush()

    async def mark_deleted(self, db_session: AsyncSession, child: ChildAccount) -> None:
        child.status = CHILD_STATUS_DELETED
        child.current_team_id = None
        child.updated_at = get_now()
        await db_session.flush()

    async def record_event(
        self,
        db_session: AsyncSession,
        *,
        email: str,
        action: str,
        team_id: Optional[int] = None,
        child_id: Optional[int] = None,
        success: bool = True,
        detail: str = "",
    ) -> None:
        db_session.add(
            SeatEvent(
                child_account_id=child_id,
                team_id=team_id,
                email=normalize_email(email),
                action=action,
                success=success,
                detail=(detail or "")[:4000],
            )
        )
        await db_session.flush()

    async def dashboard_cards(self, db_session: AsyncSession) -> List[Dict[str, Any]]:
        teams = (await db_session.execute(select(Team).order_by(Team.id.asc()))).scalars().all()
        children = (await db_session.execute(
            select(ChildAccount).where(ChildAccount.status != CHILD_STATUS_DELETED)
        )).scalars().all()
        by_team: Dict[int, List[ChildAccount]] = {}
        for child in children:
            if child.current_team_id:
                by_team.setdefault(child.current_team_id, []).append(child)

        cards = []
        for team in teams:
            members = by_team.get(team.id, [])
            active = [item for item in members if item.status in ACTIVE_CHILD_STATUSES]
            due = []
            now = get_now()
            for item in active:
                if not item.joined_at:
                    continue
                cycle_days = int(item.cycle_days or team.seat_cycle_days or 7)
                if item.joined_at + timedelta(days=cycle_days) <= now:
                    due.append(self.serialize(item))
            cards.append({
                "id": team.id,
                "email": team.email,
                "team_name": team.team_name,
                "status": team.status,
                "current_members": team.current_members,
                "max_members": team.max_members,
                "available_seats": max(0, int(team.max_members or 0) - int(team.current_members or 0)),
                "proxy": team.proxy or "",
                "seat_cycle_days": team.seat_cycle_days or 7,
                "last_sync": team.last_sync.isoformat() if team.last_sync else None,
                "active_children": [self.serialize(item) for item in active],
                "due_children": due,
            })
        return cards

    async def stats(self, db_session: AsyncSession) -> Dict[str, int]:
        result = await db_session.execute(
            select(ChildAccount.status, func.count(ChildAccount.id))
            .where(ChildAccount.status != CHILD_STATUS_DELETED)
            .group_by(ChildAccount.status)
        )
        counts = {status: count for status, count in result.all()}
        return {
            "unused": int(counts.get(CHILD_STATUS_UNUSED, 0)),
            "invited": int(counts.get(CHILD_STATUS_INVITED, 0)),
            "active": int(counts.get(CHILD_STATUS_ACTIVE, 0)),
            "standby": int(counts.get(CHILD_STATUS_STANDBY, 0)),
            "disabled": int(counts.get(CHILD_STATUS_DISABLED, 0)),
            "due": len(await self.list_due_accounts(db_session)),
        }


child_account_service = ChildAccountService()
