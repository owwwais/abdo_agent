"""منطق التحقق من رموز Supabase بمفتاح ES256 محلي وJWKS محاكى. لا يثبت التكامل مع مشروع Supabase حقيقي
(ذلك live-blocked حتى توفر المشروع)، لكنه يثبت فحوص التوقيع وiss وaud وexp والخوارزمية."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from app.api.errors import Unauthenticated
from app.auth.providers import AuthUnavailable, SupabaseAuth
from tests.conftest import make_settings

PROJECT = "https://proj.supabase.co"
KEY = ec.generate_private_key(ec.SECP256R1())


def jwks(kid: str = "k1") -> dict[str, Any]:
    jwk = json.loads(ECAlgorithm.to_jwk(KEY.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return {"keys": [jwk]}


def token(**overrides: Any) -> str:
    claims = {
        "sub": str(uuid.uuid4()),
        "email": "owner@example.com",
        "aud": "authenticated",
        "iss": f"{PROJECT}/auth/v1",
        "exp": int(time.time()) + 300,
        "role": "authenticated",
    }
    kid = overrides.pop("kid", "k1")
    claims.update(overrides)
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": kid})


def auth(handler: Any) -> SupabaseAuth:
    settings = make_settings(supabase_url=PROJECT, supabase_publishable_key="sb_publishable_test")
    return SupabaseAuth(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def jwks_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/auth/v1/.well-known/jwks.json":
        return httpx.Response(200, json=jwks())
    return httpx.Response(404)


async def test_valid_token_verified() -> None:
    t = token()
    identity = await auth(jwks_handler).verify_access_token(t)
    assert str(identity.auth_user_id) == jwt.decode(t, options={"verify_signature": False})["sub"]


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "anon"},
        {"iss": "https://other.supabase.co/auth/v1"},
        {"exp": int(time.time()) - 10},
        {"kid": "unknown"},
    ],
)
async def test_invalid_claims_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(Unauthenticated):
        await auth(jwks_handler).verify_access_token(token(**bad))


async def test_symmetric_legacy_tokens_rejected() -> None:
    legacy = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "authenticated"}, "shared-secret-" * 4, algorithm="HS256"
    )
    with pytest.raises(Unauthenticated) as err:
        await auth(jwks_handler).verify_access_token(legacy)
    assert err.value.code == "unsupported_jwt_alg"


async def test_password_grant_uses_publishable_key_and_verifies() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token":
            seen["apikey"] = request.headers.get("apikey")
            seen["grant"] = request.url.params.get("grant_type")
            body = json.loads(request.content)
            if body["password"] != "correct":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": token(), "token_type": "bearer"})
        return jwks_handler(request)

    a = auth(handler)
    identity = await a.password_sign_in("owner@example.com", "correct")
    assert identity.email == "owner@example.com"
    assert seen == {"apikey": "sb_publishable_test", "grant": "password"}
    with pytest.raises(Unauthenticated) as err:
        await a.password_sign_in("owner@example.com", "wrong")
    assert err.value.code == "invalid_credentials"


async def test_jwks_outage_is_reported_not_bypassed() -> None:
    with pytest.raises(AuthUnavailable):
        await auth(lambda r: httpx.Response(503)).verify_access_token(token())
