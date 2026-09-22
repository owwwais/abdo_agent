from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import Settings


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(settings: Settings, token: str) -> bytes:
    key = settings.session_secret.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).digest()


def data_hash(settings: Settings, kind: str, normalized_value: str) -> bytes:
    """بصمة HMAC لقيمة طبيعية (بريد، هاتف، نطاق). تفصل الأنواع لمنع التصادم بينها."""
    key = settings.data_hash_key.get_secret_value().encode()
    return hmac.new(key, f"{kind}:{normalized_value}".encode(), hashlib.sha256).digest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
