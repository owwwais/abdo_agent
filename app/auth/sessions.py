"""جلسات الخادم. الكعكة HttpOnly تحمل رمزًا عشوائيًا، والتحقق من العضوية يتم مع كل طلب."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Forbidden
from app.auth.security import hash_session_token, new_token
from app.config import Settings
from app.db.models import Membership, UserProfile, WebSession

SESSION_COOKIE = "sa_session"
_TOUCH_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class Principal:
    auth_user_id: uuid.UUID
    workspace_id: uuid.UUID
    role: str
    email: str
    display_name: str
    session_id: uuid.UUID
    csrf_token: str
    auth_method: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def actor_id(self) -> str:
        return f"user:{self.auth_user_id}"


def _now() -> datetime:
    return datetime.now(UTC)


async def active_membership(
    db: AsyncSession, auth_user_id: uuid.UUID, workspace_id: uuid.UUID | None = None
) -> Membership | None:
    q = select(Membership).where(
        Membership.auth_user_id == auth_user_id, Membership.status == "active"
    )
    if workspace_id is not None:
        q = q.where(Membership.workspace_id == workspace_id)
    q = q.order_by(Membership.created_at).limit(1)
    return (await db.execute(q)).scalar_one_or_none()


async def create_session(
    db: AsyncSession, settings: Settings, auth_user_id: uuid.UUID, auth_method: str
) -> str:
    """ينشئ جلسة لعضو فعّال ويعيد الرمز الخام (يوضع في الكعكة فقط)."""
    membership = await active_membership(db, auth_user_id)
    if membership is None:
        raise Forbidden("لا توجد عضوية فعالة لهذا الحساب", code="no_active_membership")
    token = new_token()
    db.add(
        WebSession(
            token_hash=hash_session_token(settings, token),
            auth_user_id=auth_user_id,
            workspace_id=membership.workspace_id,
            csrf_token=new_token(),
            auth_method=auth_method,
            expires_at=_now() + timedelta(days=settings.session_absolute_days),
        )
    )
    await db.flush()
    return token


async def load_principal(db: AsyncSession, settings: Settings, token: str) -> Principal | None:
    if not token or len(token) > 200:
        return None
    now = _now()
    row = (
        await db.execute(
            select(WebSession, Membership, UserProfile)
            .join(
                Membership,
                (Membership.auth_user_id == WebSession.auth_user_id)
                & (Membership.workspace_id == WebSession.workspace_id),
            )
            .join(UserProfile, UserProfile.auth_user_id == WebSession.auth_user_id)
            .where(
                WebSession.token_hash == hash_session_token(settings, token),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
                Membership.status == "active",
            )
        )
    ).one_or_none()
    if row is None:
        return None
    ws, membership, profile = row
    if ws.last_seen_at + timedelta(hours=settings.session_idle_hours) < now:
        return None
    if now - ws.last_seen_at > _TOUCH_INTERVAL:
        await db.execute(update(WebSession).where(WebSession.id == ws.id).values(last_seen_at=now))
        await db.commit()
    return Principal(
        auth_user_id=profile.auth_user_id,
        workspace_id=ws.workspace_id,
        role=membership.role,
        email=profile.email,
        display_name=profile.display_name,
        session_id=ws.id,
        csrf_token=ws.csrf_token,
        auth_method=ws.auth_method,
    )


async def revoke_session(db: AsyncSession, settings: Settings, token: str) -> None:
    await db.execute(
        update(WebSession)
        .where(WebSession.token_hash == hash_session_token(settings, token))
        .values(revoked_at=_now())
    )
