"""أسرار التكاملات: مشفرة بـFernet في القاعدة، مع احتياط من متغيرات البيئة.

القيم لا تُعرض ولا تُسجل ولا تُعاد في أي استجابة. الواجهة ترى فقط: مضبوط/غير مضبوط ومصدره.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models_ops import IntegrationSecret
from app.services import audit


@dataclass(frozen=True)
class SecretSpec:
    label: str
    env_attr: str | None


SECRETS: dict[str, SecretSpec] = {
    "anthropic_api_key": SecretSpec("مفتاح Anthropic (Claude)", "anthropic_api_key"),
    "openai_api_key": SecretSpec("مفتاح OpenAI", "openai_api_key"),
    "gemini_api_key": SecretSpec("مفتاح Google Gemini", "gemini_api_key"),
    "openai_compatible_api_key": SecretSpec(
        "مفتاح مزود متوافق مع OpenAI", "openai_compatible_api_key"
    ),
    "brave_api_key": SecretSpec("مفتاح Brave Search", "brave_search_api_key"),
    "smtp_password": SecretSpec("كلمة مرور SMTP", "smtp_password"),
    "imap_password": SecretSpec("كلمة مرور IMAP", "imap_password"),
    "telegram_bot_token": SecretSpec("رمز بوت تيليجرام", "telegram_bot_token"),
    # يولده التطبيق عند ربط الويب هوك؛ لا يُدخله المستخدم.
    "telegram_webhook_secret": SecretSpec("سر ويب هوك تيليجرام", None),
    "hostinger_webhook_secret": SecretSpec(
        "سر ويب هوك Hostinger (Bearer)", "hostinger_webhook_secret"
    ),
}

Source = Literal["settings", "env", "none"]


class SecretBox:
    def __init__(self, raw_keys: str) -> None:
        keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode("utf-8")


def box(settings: Settings) -> SecretBox:
    return SecretBox(settings.secrets_encryption_key.get_secret_value())


def _env_value(settings: Settings, key: str) -> str:
    spec = SECRETS[key]
    if spec.env_attr is None:
        return ""
    raw = getattr(settings, spec.env_attr)
    return raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw or "")


async def get_secret(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, key: str
) -> tuple[str, Source]:
    """القيمة الفعالة ومصدرها: صفحة الإعدادات أولًا ثم ملف البيئة."""
    if key not in SECRETS:
        raise KeyError(key)
    row = (
        await db.execute(
            select(IntegrationSecret.ciphertext).where(
                IntegrationSecret.workspace_id == workspace_id, IntegrationSecret.key == key
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        try:
            return box(settings).decrypt(row), "settings"
        except InvalidToken:
            # مفتاح تشفير تغير دون الاحتفاظ بالقديم؛ نعامله كغير مضبوط بدل تسريب خطأ.
            return "", "none"
    env = _env_value(settings, key)
    return (env, "env") if env else ("", "none")


async def secret_status(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> dict[str, Source]:
    stored = set(
        (
            await db.execute(
                select(IntegrationSecret.key).where(IntegrationSecret.workspace_id == workspace_id)
            )
        ).scalars()
    )
    out: dict[str, Source] = {}
    for key in SECRETS:
        if key in stored:
            out[key] = "settings"
        elif _env_value(settings, key):
            out[key] = "env"
        else:
            out[key] = "none"
    return out


async def set_secret(
    db: AsyncSession,
    settings: Settings,
    workspace_id: uuid.UUID,
    key: str,
    value: str,
    actor_id: str,
) -> None:
    if key not in SECRETS:
        raise KeyError(key)
    value = value.strip()
    if not value:
        return
    stmt = insert(IntegrationSecret).values(
        workspace_id=workspace_id,
        key=key,
        ciphertext=box(settings).encrypt(value),
        updated_by=actor_id,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[IntegrationSecret.workspace_id, IntegrationSecret.key],
            set_={"ciphertext": stmt.excluded.ciphertext, "updated_by": actor_id},
        )
    )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="secret.set",
        entity_type="integration_secret",
        change={"key": key},
    )


async def delete_secret(db: AsyncSession, workspace_id: uuid.UUID, key: str, actor_id: str) -> None:
    await db.execute(
        delete(IntegrationSecret).where(
            IntegrationSecret.workspace_id == workspace_id, IntegrationSecret.key == key
        )
    )
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="secret.delete",
        entity_type="integration_secret",
        change={"key": key},
    )
