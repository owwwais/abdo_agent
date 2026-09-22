"""مزودو الهوية: Supabase Auth للبيئات الحقيقية، وfixture محلي للتطوير فقط.

Supabase: تسجيل الدخول بكلمة المرور عبر POST /auth/v1/token?grant_type=password مع ترويسة apikey
(المفتاح المنشور sb_publishable_...)، ثم التحقق من access token محليًا بمفاتيح JWKS العامة
{SUPABASE_URL}/auth/v1/.well-known/jwks.json (ES256/RS256)، والتحقق من iss وaud وexp.
لا تُحفظ رموز Supabase بعد التحقق؛ الجلسة بعدها جلسة خادم مرتبطة بعضوية فعالة.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.api.errors import AppError, Unauthenticated
from app.config import Settings

_ALLOWED_ALGS = ["ES256", "RS256"]
_JWKS_TTL_SECONDS = 600  # توصية Supabase: لا تخزن JWKS أطول من 10 دقائق.


class AuthUnavailable(AppError):
    status_code = 503
    code = "auth_unavailable"
    message = "خدمة الدخول غير متاحة حاليًا"


@dataclass(frozen=True)
class VerifiedIdentity:
    auth_user_id: uuid.UUID
    email: str


class SupabaseAuth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base = settings.supabase_url.rstrip("/")
        self._http = http
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at = 0.0

    @property
    def issuer(self) -> str:
        return f"{self._base}/auth/v1"

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._http

    async def _jwk_set(self, force: bool = False) -> jwt.PyJWKSet:
        if force or self._jwks is None or time.monotonic() - self._jwks_at > _JWKS_TTL_SECONDS:
            client = await self._client()
            try:
                resp = await client.get(f"{self.issuer}/.well-known/jwks.json")
                resp.raise_for_status()
                self._jwks = jwt.PyJWKSet.from_dict(resp.json())
            except (httpx.HTTPError, jwt.PyJWKSetError, ValueError) as exc:
                raise AuthUnavailable("تعذر جلب مفاتيح التحقق من Supabase") from exc
            self._jwks_at = time.monotonic()
        return self._jwks

    async def verify_access_token(self, token: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise Unauthenticated("رمز الدخول غير صالح") from exc
        if header.get("alg") not in _ALLOWED_ALGS:
            # HS256 (السر المشترك القديم) غير مقبول؛ يلزم تفعيل مفاتيح توقيع غير متماثلة في المشروع.
            raise Unauthenticated("خوارزمية توقيع غير مقبولة", code="unsupported_jwt_alg")
        kid = header.get("kid")
        key = self._find_key(await self._jwk_set(), kid)
        if key is None:  # ربما دُوّرت المفاتيح
            key = self._find_key(await self._jwk_set(force=True), kid)
        if key is None:
            raise Unauthenticated("مفتاح التوقيع غير معروف")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=_ALLOWED_ALGS,
                audience=self._settings.supabase_jwt_audience,
                issuer=self.issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
            return VerifiedIdentity(uuid.UUID(claims["sub"]), str(claims.get("email", "")))
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise Unauthenticated("رمز الدخول غير صالح أو منتهي") from exc

    @staticmethod
    def _find_key(jwks: jwt.PyJWKSet, kid: str | None) -> jwt.PyJWK | None:
        for k in jwks.keys:
            if k.key_id == kid:
                return k
        return None

    async def password_sign_in(self, email: str, password: str) -> VerifiedIdentity:
        client = await self._client()
        try:
            resp = await client.post(
                f"{self.issuer}/token",
                params={"grant_type": "password"},
                headers={"apikey": self._settings.supabase_publishable_key},
                json={"email": email, "password": password},
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable() from exc
        if resp.status_code in (400, 401, 422):
            raise Unauthenticated("البريد أو كلمة المرور غير صحيحة", code="invalid_credentials")
        if resp.status_code != 200:
            raise AuthUnavailable()
        token = resp.json().get("access_token")
        if not isinstance(token, str):
            raise AuthUnavailable()
        return await self.verify_access_token(token)


# مستخدمو fixture للتطوير: معرفات ثابتة كي تبقى العضويات بعد إعادة seed.
DEV_USERS: dict[str, dict[str, str]] = {
    "owner": {
        "auth_user_id": "00000000-0000-4000-8000-00000000d001",
        "email": "owner@demo.local",
        "display_name": "مالك تجريبي",
        "role": "owner",
    },
    "reviewer": {
        "auth_user_id": "00000000-0000-4000-8000-00000000d002",
        "email": "reviewer@demo.local",
        "display_name": "مراجع تجريبي",
        "role": "reviewer",
    },
}


def dev_auth_allowed(settings: Settings, client_host: str | None) -> bool:
    """fixture الدخول لا يعمل إلا محليًا: بيئة محلية + مفعّل صراحة + طلب من loopback."""
    return (
        settings.dev_auth_enabled
        and settings.app_env.is_local
        and client_host in ("127.0.0.1", "::1")
    )
