from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

# حقول لا تُكتب قيمها في سجل التدقيق.
_SENSITIVE = {"value", "email", "phone", "password", "token", "credential", "contacts"}


def redact(change: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in change.items():
        if any(s in key.lower() for s in _SENSITIVE):
            out[key] = "[محجوب]"
        elif isinstance(val, uuid.UUID):
            out[key] = str(val)
        elif isinstance(val, str) and len(val) > 300:
            out[key] = val[:300] + "…"
        else:
            out[key] = val
    return out


def record(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    actor_id: str,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    entity_version: int | None = None,
    change: dict[str, Any] | None = None,
) -> None:
    actor_type = actor_id.split(":", 1)[0] if ":" in actor_id else "system"
    db.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type=actor_type if actor_type in ("user", "service", "system") else "system",
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_version=entity_version,
            redacted_change=redact(change or {}),
        )
    )
