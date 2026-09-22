"""تطبيق FastAPI: واجهة عربية + JSON API + فحوص الصحة.

uv run python -m app.main          # تشغيل محلي (يضبط حلقة الأحداث المناسبة في Windows)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.errors import AppError, Unauthenticated
from app.api.routes import router as api_router
from app.api.sales_api import router as sales_api_router
from app.api.webhooks import router as webhooks_router
from app.auth.providers import SupabaseAuth
from app.config import Settings, get_settings
from app.db.session import create_engine, loop_factory, make_sessionmaker
from app.services.common import validation_details
from app.web.pages import router as pages_router
from app.web.render import render
from app.web.sales_pages import router as sales_pages_router
from app.web.settings_pages import router as settings_router

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith(
        ("/api/", "/webhooks/", "/health/")
    ) or "application/json" in request.headers.get("accept", "")


def create_app(settings: Settings | None = None, engine: AsyncEngine | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_engine = engine is None
        app.state.engine = engine or create_engine(settings)
        app.state.sessionmaker = make_sessionmaker(app.state.engine)
        try:
            yield
        finally:
            if own_engine:
                await app.state.engine.dispose()

    app = FastAPI(
        title="وكيل البحث والمبيعات",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.app_env.is_local else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.app_env.is_local else None,
    )
    app.state.settings = settings
    app.state.supabase = SupabaseAuth(settings) if settings.supabase_configured else None
    if engine is not None:  # الاختبارات تمرر engine ولا تشغل lifespan
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> Response:
        if _wants_json(request):
            return JSONResponse(exc.to_dict(), status_code=exc.status_code)
        if isinstance(exc, Unauthenticated):
            return RedirectResponse("/login", status_code=303)
        return render(request, "error.html", status_code=exc.status_code, error=exc, flash=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        err = AppError("بيانات غير صالحة", code="invalid_input", details=validation_details(exc))
        err.status_code = 422
        return await app_error(request, err)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> Response:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1 FROM sales.workspaces LIMIT 1"))
        except Exception:
            log.exception("readiness check failed")
            return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok", "env": settings.app_env.value})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api_router)
    app.include_router(sales_api_router)
    app.include_router(webhooks_router)
    app.include_router(pages_router)
    app.include_router(settings_router)
    app.include_router(sales_pages_router)
    return app


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    logging.basicConfig(level=s.log_level)
    config = uvicorn.Config(
        "app.main:create_app", factory=True, host="127.0.0.1", port=8000, proxy_headers=False
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve(), loop_factory=loop_factory())
