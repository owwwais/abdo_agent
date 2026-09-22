from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import CsrfFailed, Unauthenticated
from app.auth.security import constant_time_equals
from app.auth.sessions import SESSION_COOKIE, Principal, load_principal
from app.config import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """جلسة قاعدة لكل طلب. التعديلات تُعتمد صراحة داخل المعالج؛ غير ذلك rollback عند الإغلاق."""
    async with request.app.state.sessionmaker() as session:
        yield session


async def optional_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await load_principal(db, settings, token)


def _origin_ok(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin != "null"
    expected = {
        f"{urlsplit(settings.app_base_url).scheme}://{urlsplit(settings.app_base_url).netloc}"
    }
    expected.add(f"{request.url.scheme}://{request.url.netloc}")
    return origin in expected


async def current_principal(
    request: Request,
    principal: Principal | None = Depends(optional_principal),
    settings: Settings = Depends(get_settings_dep),
) -> Principal:
    if principal is None:
        raise Unauthenticated()
    if request.method not in SAFE_METHODS:
        # حماية CSRF لكل تعديل يعتمد على كعكة الجلسة: رمز مرتبط بالجلسة + فحص Origin إن وُجد.
        if not _origin_ok(request, settings):
            raise CsrfFailed()
        supplied = request.headers.get(CSRF_HEADER)
        if supplied is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get(CSRF_FIELD)
            supplied = value if isinstance(value, str) else None
        if not supplied or not constant_time_equals(supplied, principal.csrf_token):
            raise CsrfFailed()
    request.state.principal = principal
    return principal
