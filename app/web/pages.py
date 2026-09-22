"""صفحات HTML عربية. النماذج ترسل POST ثم تعيد التوجيه؛ الأخطاء تعرض داخل النموذج نفسه."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile

from app.api import serialize as ser
from app.api.deps import current_principal, get_db, get_settings_dep, optional_principal
from app.api.errors import AppError, CsrfFailed, InvalidInput
from app.auth.providers import DEV_USERS, dev_auth_allowed
from app.auth.security import constant_time_equals, new_token
from app.auth.sessions import SESSION_COOKIE, Principal, create_session, revoke_session
from app.config import Settings
from app.db.models import (
    Company,
    CompanyDuplicateCandidate,
    CompanyIdentifier,
    CompanySourceLink,
    Contact,
    ImportBatch,
    Job,
    Membership,
    Product,
    Segment,
    Source,
    SourceSegment,
    UserProfile,
    WorkerHeartbeat,
    Workspace,
)
from app.services import imports as import_service
from app.services import products as product_service
from app.services import sources as source_service
from app.services.common import get_scoped, lines_to_list, validation_details
from app.web.render import render

router = APIRouter(include_in_schema=False)
LOGIN_CSRF_COOKIE = "sa_login_csrf"


def _to_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return {
            "message": "راجع الحقول المحددة",
            "fields": validation_details(exc).get("fields", {}),
        }
    if isinstance(exc, AppError):
        fields = exc.details.get("fields", {}) if exc.details else {}
        return {
            "message": exc.message,
            "fields": fields,
            "problems": exc.details.get("problems", []) if exc.details else [],
        }
    raise exc


def _status(exc: Exception) -> int:
    return exc.status_code if isinstance(exc, AppError) else 422


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_absolute_days * 86400,
        path="/",
    )


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _form_int(form: FormData, key: str) -> int | None:
    raw = _form_str(form, key)
    return int(raw) if raw.lstrip("-").isdigit() else None


# ---------------------------------------------------------------- الدخول


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    principal: Principal | None = Depends(optional_principal),
) -> Response:
    if principal is not None:
        return RedirectResponse("/", status_code=303)
    token = request.cookies.get(LOGIN_CSRF_COOKIE) or new_token()
    client = request.client.host if request.client else None
    response = render(
        request,
        "login.html",
        principal=None,
        login_csrf=token,
        dev_enabled=dev_auth_allowed(settings, client),
        dev_users=DEV_USERS,
        supabase_enabled=settings.supabase_configured,
        error=request.query_params.get("error"),
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=3600,
        path="/",
    )
    return response


async def _check_login_csrf(request: Request) -> FormData:
    form = await request.form()
    supplied = _form_str(form, "login_csrf")
    expected = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not supplied or not expected or not constant_time_equals(supplied, expected):
        raise CsrfFailed()
    return form


@router.post("/login")
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await _check_login_csrf(request)
    auth = request.app.state.supabase
    if auth is None:
        return RedirectResponse("/login?error=auth_not_configured", status_code=303)
    try:
        identity = await auth.password_sign_in(
            _form_str(form, "email"), _form_str(form, "password")
        )
        token = await create_session(db, settings, identity.auth_user_id, "supabase")
        await db.commit()
    except AppError as exc:
        return RedirectResponse(f"/login?error={exc.code}", status_code=303)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, settings, token)
    return response


@router.post("/auth/dev-login")
async def dev_login(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    client = request.client.host if request.client else None
    if not dev_auth_allowed(settings, client):
        raise AppError("دخول التطوير غير متاح هنا", code="dev_auth_disabled")
    form = await _check_login_csrf(request)
    user = DEV_USERS.get(_form_str(form, "user"))
    if user is None:
        raise InvalidInput("مستخدم تجريبي غير معروف")
    token = await create_session(db, settings, uuid.UUID(user["auth_user_id"]), "dev")
    await db.commit()
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, settings, token)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    await revoke_session(db, settings, request.cookies.get(SESSION_COOKIE, ""))
    await db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- اليوم


@router.get("/", response_class=HTMLResponse)
async def today(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    ws = p.workspace_id

    async def count(model: Any, *where: Any) -> int:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(model.workspace_id == ws, *where)
                )
            ).scalar_one()
        )

    products_active = await count(Product, Product.status == "active")
    sources_active = await count(Source, Source.status == "active")
    segments_active = await count(Segment, Segment.status == "active")
    companies_total = await count(Company)
    companies_review = await count(Company, Company.status == "needs_review")
    jobs_pending = await count(Job, Job.status.in_(("queued", "running", "retry_scheduled")))
    jobs_failed = await count(Job, Job.status == "failed")
    problem_sources = list(
        (
            await db.execute(
                select(Source)
                .where(
                    Source.workspace_id == ws,
                    Source.status.in_(("failed", "restricted", "needs_setup")),
                )
                .order_by(Source.updated_at.desc())
                .limit(5)
            )
        ).scalars()
    )
    heartbeat = (
        await db.execute(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.stopped_at.is_(None))
            .order_by(WorkerHeartbeat.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    worker_alive = heartbeat is not None and heartbeat.last_seen_at > datetime.now(UTC) - timedelta(
        minutes=2
    )
    setup = [
        ("فئة مستهدفة نشطة", segments_active > 0, "/segments"),
        ("منتج نشط", products_active > 0, "/products"),
        ("مصدر نشط", sources_active > 0, "/sources"),
    ]
    workspace = await db.get(Workspace, ws)
    return render(
        request,
        "today.html",
        setup=setup,
        products_active=products_active,
        sources_active=sources_active,
        companies_total=companies_total,
        companies_review=companies_review,
        jobs_pending=jobs_pending,
        jobs_failed=jobs_failed,
        problem_sources=problem_sources,
        heartbeat=heartbeat,
        worker_alive=worker_alive,
        workspace=workspace,
    )


# ---------------------------------------------------------------- الفئات


@router.get("/segments", response_class=HTMLResponse)
async def segments_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return render(
        request,
        "segments.html",
        segments=await product_service.list_segments(db, p, include_archived=True),
    )


def _segment_values(form: FormData) -> dict[str, Any]:
    return {
        "name": _form_str(form, "name"),
        "description": _form_str(form, "description"),
        "fit_rules": lines_to_list(_form_str(form, "fit_rules")),
        "exclusion_rules": lines_to_list(_form_str(form, "exclusion_rules")),
    }


@router.post("/segments")
async def segment_create(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    form = await request.form()
    try:
        await product_service.create_segment(
            db, p, product_service.SegmentInput(**_segment_values(form))
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return render(
            request,
            "segments.html",
            status_code=_status(exc),
            error=_to_error(exc),
            form=form,
            segments=await product_service.list_segments(db, p, include_archived=True),
        )
    return RedirectResponse("/segments?ok=segment_created", status_code=303)


@router.post("/segments/{segment_id}")
async def segment_update(
    segment_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    values: dict[str, Any] = {"version": _form_int(form, "version") or 0, **_segment_values(form)}
    status = _form_str(form, "status")
    if status:
        values["status"] = status
    try:
        await product_service.update_segment(
            db, p, segment_id, product_service.SegmentPatch(**values)
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return render(
            request,
            "segments.html",
            status_code=_status(exc),
            error=_to_error(exc),
            edit_id=segment_id,
            segments=await product_service.list_segments(db, p, include_archived=True),
        )
    return RedirectResponse("/segments?ok=segment_saved", status_code=303)


# ---------------------------------------------------------------- المنتجات


@router.get("/products", response_class=HTMLResponse)
async def products_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return render(request, "products.html", products=await product_service.list_products(db, p))


def _product_values(form: FormData) -> dict[str, Any]:
    regions = [
        r.strip() for r in _form_str(form, "regions").replace("،", ",").split(",") if r.strip()
    ]
    return {
        "name": _form_str(form, "name"),
        "summary": _form_str(form, "summary"),
        "problem": _form_str(form, "problem"),
        "capabilities": lines_to_list(_form_str(form, "capabilities")),
        "unavailable_capabilities": lines_to_list(_form_str(form, "unavailable_capabilities")),
        "fit_signals": lines_to_list(_form_str(form, "fit_signals")),
        "exclusions": lines_to_list(_form_str(form, "exclusions")),
        "product_url": _form_str(form, "product_url") or None,
        "demo_url": _form_str(form, "demo_url") or None,
        "price_status": _form_str(form, "price_status") or "needs_review",
        "price_text": _form_str(form, "price_text"),
        "priority": _form_int(form, "priority") or 3,
        "segments": [
            {"segment_id": sid, "regions": regions}
            for sid in form.getlist("segment_ids")
            if isinstance(sid, str)
        ],
    }


async def _product_form(
    request: Request,
    p: Principal,
    db: AsyncSession,
    product: Product | None,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    segments = await product_service.list_segments(db, p)
    linked: list[Any] = []
    problems: list[str] = []
    if product is not None:
        linked = await product_service.product_segments(db, p, product.id)
        problems = await product_service.activation_problems(db, product)
    return render(
        request,
        "product_form.html",
        status_code=status_code,
        product=product,
        segments=segments,
        linked_ids={str(s.id) for _, s in linked},
        regions=", ".join(linked[0][0].regions) if linked else "",
        problems=problems,
        **extra,
    )


@router.get("/products/new", response_class=HTMLResponse)
async def product_new(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return await _product_form(request, p, db, None)


@router.post("/products")
async def product_create(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    form = await request.form()
    try:
        product = await product_service.create_product(
            db, p, product_service.ProductInput(**_product_values(form))
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _product_form(
            request, p, db, None, _status(exc), error=_to_error(exc), form=form
        )
    return RedirectResponse(f"/products/{product.id}?ok=product_created", status_code=303)


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail(
    product_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    return await _product_form(
        request, p, db, await get_scoped(db, Product, p.workspace_id, product_id)
    )


@router.post("/products/{product_id}")
async def product_update(
    product_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        values = {"version": _form_int(form, "version") or 0, **_product_values(form)}
        await product_service.update_product(
            db, p, product_id, product_service.ProductPatch(**values)
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        product = await get_scoped(db, Product, p.workspace_id, product_id)
        return await _product_form(
            request, p, db, product, _status(exc), error=_to_error(exc), form=form
        )
    return RedirectResponse(f"/products/{product_id}?ok=product_saved", status_code=303)


@router.post("/products/{product_id}/status")
async def product_status(
    product_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        target = _form_str(form, "status")
        if target not in ("draft", "active", "paused", "archived"):
            raise InvalidInput("حالة غير معروفة")
        await product_service.change_product_status(
            db,
            p,
            product_id,
            cast(product_service.ProductStatus, target),
            _form_int(form, "version") or 0,
        )
        await db.commit()
    except AppError as exc:
        await db.rollback()
        product = await get_scoped(db, Product, p.workspace_id, product_id)
        return await _product_form(request, p, db, product, _status(exc), error=_to_error(exc))
    return RedirectResponse(f"/products/{product_id}?ok=product_status", status_code=303)


# ---------------------------------------------------------------- المصادر


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return render(request, "sources.html", sources=await source_service.list_sources(db, p))


def _source_values(form: FormData, *, include_kind: bool) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": _form_str(form, "name"),
        "url": _form_str(form, "url") or None,
        "allowed_hosts": lines_to_list(_form_str(form, "allowed_hosts")),
        "allowed_paths": lines_to_list(_form_str(form, "allowed_paths")),
        "credential_ref": _form_str(form, "credential_ref") or None,
        "store_raw": _form_str(form, "store_raw") == "on",
        "policy_notes": _form_str(form, "policy_notes"),
        "segments": [
            {"segment_id": sid} for sid in form.getlist("segment_ids") if isinstance(sid, str)
        ],
    }
    for key in (
        "max_pages",
        "max_records",
        "max_requests",
        "timeout_seconds",
        "refresh_interval_hours",
        "retention_days",
    ):
        raw = _form_str(form, key)
        if raw:
            values[key] = raw
    if include_kind:
        values["kind"] = _form_str(form, "kind")
    return values


async def _source_form(
    request: Request, p: Principal, db: AsyncSession, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return render(
        request,
        "source_new.html",
        status_code=status_code,
        segments=await product_service.list_segments(db, p),
        kinds=source_service.SOURCE_KINDS,
        **extra,
    )


@router.get("/sources/new", response_class=HTMLResponse)
async def source_new(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return await _source_form(request, p, db)


@router.post("/sources")
async def source_create(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        src = await source_service.create_source(
            db, p, settings, source_service.SourceInput(**_source_values(form, include_kind=True))
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _source_form(request, p, db, _status(exc), error=_to_error(exc), form=form)
    return RedirectResponse(f"/sources/{src.id}?ok=source_created", status_code=303)


async def _source_detail(
    request: Request,
    p: Principal,
    db: AsyncSession,
    source_id: uuid.UUID,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    src = await get_scoped(db, Source, p.workspace_id, source_id)
    check = await source_service.latest_check(db, src)
    job = await source_service.active_sample_job(db, src)
    linked = {
        str(sid)
        for sid in (
            await db.execute(
                select(SourceSegment.segment_id).where(SourceSegment.source_id == src.id)
            )
        ).scalars()
    }
    can_activate = (
        src.status in ("sample_ready", "paused")
        and check is not None
        and check.status == "succeeded"
        and check.config_version == src.config_version
    )
    return render(
        request,
        "source_detail.html",
        status_code=status_code,
        source=src,
        check=check,
        job=job,
        segments=await product_service.list_segments(db, p),
        linked_ids=linked,
        can_activate=can_activate,
        policy_items=source_service.SOURCE_POLICY_ITEMS,
        policy_version=source_service.SOURCE_POLICY_VERSION,
        kind_spec=source_service.SOURCE_KINDS[src.kind],
        stale_validating=src.status == "validating" and job is None,
        **extra,
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_detail(
    source_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    return await _source_detail(request, p, db, source_id)


@router.post("/sources/{source_id}")
async def source_update(
    source_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        values = {
            "version": _form_int(form, "version") or 0,
            **_source_values(form, include_kind=False),
        }
        await source_service.update_source(db, p, source_id, source_service.SourcePatch(**values))
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _source_detail(
            request, p, db, source_id, _status(exc), error=_to_error(exc), form=form
        )
    return RedirectResponse(f"/sources/{source_id}?ok=source_saved", status_code=303)


@router.post("/sources/{source_id}/test")
async def source_test(
    source_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await source_service.request_test(db, p, source_id)
        await db.commit()
    except AppError as exc:
        await db.rollback()
        return await _source_detail(request, p, db, source_id, _status(exc), error=_to_error(exc))
    return RedirectResponse(f"/sources/{source_id}?ok=source_test", status_code=303)


@router.post("/sources/{source_id}/activate")
async def source_activate(
    source_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        data = source_service.ActivateInput(
            version=_form_int(form, "version") or 0,
            policy_version=_form_int(form, "policy_version") or 0,
            confirmations=[
                _form_str(form, f"confirm_{i}") == "on"
                for i in range(len(source_service.SOURCE_POLICY_ITEMS))
            ],
            policy_notes=_form_str(form, "policy_notes"),
        )
        await source_service.activate_source(db, p, source_id, data)
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _source_detail(request, p, db, source_id, _status(exc), error=_to_error(exc))
    return RedirectResponse(f"/sources/{source_id}?ok=source_active", status_code=303)


@router.post("/sources/{source_id}/pause")
async def source_pause(
    source_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        await source_service.pause_source(db, p, source_id, _form_int(form, "version") or 0)
        await db.commit()
    except AppError as exc:
        await db.rollback()
        return await _source_detail(request, p, db, source_id, _status(exc), error=_to_error(exc))
    return RedirectResponse(f"/sources/{source_id}?ok=source_paused", status_code=303)


# ---------------------------------------------------------------- الشركات


@router.get("/companies", response_class=HTMLResponse)
async def companies_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    status = request.query_params.get("status") or None
    raw_page = request.query_params.get("page", "1")
    page = max(1, int(raw_page)) if raw_page.isdigit() else 1
    per_page = 50
    q = select(Company).where(Company.workspace_id == p.workspace_id)
    if status in ("active", "needs_review", "archived"):
        q = q.where(Company.status == status)
    total = int((await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one())
    companies = list(
        (
            await db.execute(
                q.order_by(Company.created_at.desc()).limit(per_page).offset((page - 1) * per_page)
            )
        ).scalars()
    )
    segments = {
        s.id: s.name for s in await product_service.list_segments(db, p, include_archived=True)
    }
    return render(
        request,
        "companies.html",
        companies=companies,
        total=total,
        page=page,
        per_page=per_page,
        status=status,
        segment_names=segments,
    )


@router.get("/companies/{company_id}", response_class=HTMLResponse)
async def company_detail(
    company_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    c = await get_scoped(db, Company, p.workspace_id, company_id)
    identifiers = list(
        (
            await db.execute(
                select(CompanyIdentifier)
                .where(CompanyIdentifier.company_id == c.id)
                .order_by(CompanyIdentifier.strength, CompanyIdentifier.kind)
            )
        ).scalars()
    )
    links = list(
        (
            await db.execute(
                select(CompanySourceLink, Source.name)
                .join(
                    Source,
                    (Source.id == CompanySourceLink.source_id)
                    & (Source.workspace_id == CompanySourceLink.workspace_id),
                )
                .where(CompanySourceLink.company_id == c.id)
                .order_by(CompanySourceLink.first_seen_at)
            )
        ).tuples()
    )
    contacts = list((await db.execute(select(Contact).where(Contact.company_id == c.id))).scalars())
    dup_rows = list(
        (
            await db.execute(
                select(CompanyDuplicateCandidate, Company.display_name)
                .join(
                    Company,
                    (Company.id == CompanyDuplicateCandidate.candidate_company_id)
                    & (Company.workspace_id == CompanyDuplicateCandidate.workspace_id),
                )
                .where(CompanyDuplicateCandidate.company_id == c.id)
            )
        ).tuples()
    )
    segment = await db.get(Segment, c.segment_id) if c.segment_id else None
    return render(
        request,
        "company_detail.html",
        company=c,
        identifiers=identifiers,
        links=links,
        contacts=[(ct, ser.mask(ct.channel, ct.value)) for ct in contacts],
        duplicates=dup_rows,
        segment=segment,
    )


# ---------------------------------------------------------------- الاستيراد


async def _imports_page(
    request: Request, p: Principal, db: AsyncSession, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    sources = [s for s in await source_service.list_sources(db, p) if s.status == "active"]
    return render(
        request,
        "imports.html",
        status_code=status_code,
        sources=sources,
        segments=await product_service.list_segments(db, p),
        batches=await import_service.recent_batches(db, p),
        **extra,
    )


@router.get("/imports", response_class=HTMLResponse)
async def imports_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    return await _imports_page(request, p, db)


_MANUAL_FIELDS = (
    "name",
    "website",
    "sector",
    "segment",
    "region",
    "city",
    "country",
    "phone",
    "email",
    "contact_name",
    "contact_role",
    "cr_number",
    "notes",
    "source_url",
)


@router.post("/imports/manual")
async def import_manual(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        entry = import_service.ManualEntry(**{k: _form_str(form, k) for k in _MANUAL_FIELDS})
        source_id = uuid.UUID(_form_str(form, "source_id"))
        batch = await import_service.create_preview(
            db, p, settings, source_id=source_id, rows=[entry.model_dump()], kind="manual"
        )
        rows = await import_service.batch_rows(db, p, batch.id)
        if rows[0].action == "skip_error":
            raise InvalidInput(
                "بيانات الجهة غير صالحة",
                details={"fields": {e["field"]: e["message"] for e in rows[0].errors}},
            )
        batch = await import_service.commit_batch(db, p, settings, batch.id)
        await db.commit()
    except (ValidationError, AppError, ValueError) as exc:
        await db.rollback()
        if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
            exc = InvalidInput("اختر مصدرًا نشطًا")
        return await _imports_page(request, p, db, _status(exc), error=_to_error(exc), form=form)
    return RedirectResponse(f"/imports/{batch.id}?ok=manual_added", status_code=303)


@router.post("/imports/csv")
async def import_csv(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise InvalidInput("اختر ملف CSV")
        raw = await upload.read(import_service.MAX_FILE_BYTES + 1)
        if not raw:
            raise InvalidInput("الملف فارغ", code="empty_file")
        rows, ignored = import_service.read_rows(import_service.decode_csv(raw))
        try:
            source_id = uuid.UUID(_form_str(form, "source_id"))
        except ValueError as exc:
            raise InvalidInput("اختر مصدرًا نشطًا") from exc
        batch = await import_service.create_preview(
            db,
            p,
            settings,
            source_id=source_id,
            rows=rows,
            kind="csv",
            filename=upload.filename,
            ignored_columns=ignored,
        )
        await db.commit()
    except AppError as exc:
        await db.rollback()
        return await _imports_page(request, p, db, _status(exc), error=_to_error(exc))
    return RedirectResponse(f"/imports/{batch.id}", status_code=303)


@router.get("/imports/{batch_id}", response_class=HTMLResponse)
async def import_batch(
    batch_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id)
    rows = await import_service.batch_rows(db, p, batch_id)
    source = await db.get(Source, batch.source_id)
    return render(request, "import_batch.html", batch=batch, rows=rows, source=source)


@router.post("/imports/{batch_id}/commit")
async def import_commit(
    batch_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    try:
        await import_service.commit_batch(db, p, settings, batch_id)
        await db.commit()
    except AppError as exc:
        await db.rollback()
        batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id)
        return render(
            request,
            "import_batch.html",
            status_code=_status(exc),
            batch=batch,
            error=_to_error(exc),
            rows=await import_service.batch_rows(db, p, batch_id),
            source=await db.get(Source, batch.source_id),
        )
    return RedirectResponse(f"/imports/{batch_id}?ok=import_committed", status_code=303)


@router.post("/imports/{batch_id}/cancel")
async def import_cancel(
    batch_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await import_service.cancel_batch(db, p, batch_id)
        await db.commit()
    except AppError as exc:
        await db.rollback()
        batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id)
        return render(
            request,
            "import_batch.html",
            status_code=_status(exc),
            batch=batch,
            error=_to_error(exc),
            rows=await import_service.batch_rows(db, p, batch_id),
            source=await db.get(Source, batch.source_id),
        )
    return RedirectResponse(f"/imports/{batch_id}?ok=import_canceled", status_code=303)


# ---------------------------------------------------------------- الإعدادات والتشغيل


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    members = list(
        (
            await db.execute(
                select(Membership, UserProfile)
                .join(UserProfile, UserProfile.auth_user_id == Membership.auth_user_id)
                .where(Membership.workspace_id == p.workspace_id)
                .order_by(Membership.role, UserProfile.display_name)
            )
        ).tuples()
    )
    workers = list(
        (
            await db.execute(
                select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(5)
            )
        ).scalars()
    )
    failed_jobs = list(
        (
            await db.execute(
                select(Job)
                .where(Job.workspace_id == p.workspace_id, Job.status == "failed")
                .order_by(Job.updated_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    integrations = [
        (
            "هوية المؤسسين (Supabase Auth)",
            "مهيأ" if settings.supabase_configured else "غير مهيأ",
            "دخول التطوير المحلي فقط" if not settings.supabase_configured else "التحقق عبر JWKS",
        ),
        (
            "بحث الويب",
            "FakeSearch اصطناعي"
            if settings.search_provider == "fake"
            else settings.search_provider,
            "لم يُختر مزود حقيقي بعد",
        ),
        (
            "النماذج اللغوية",
            "FakeModel" if settings.model_provider == "fake" else settings.model_provider,
            "تُبنى في M2",
        ),
        (
            "البريد",
            "FakeMailbox" if settings.mail_provider == "fake" else settings.mail_provider,
            "تُبنى في M4",
        ),
        (
            "تيليجرام",
            "مهيأ" if settings.telegram_bot_token.get_secret_value() else "غير مهيأ",
            "يُبنى في M3",
        ),
        ("الإرسال الخارجي", "مفعّل" if settings.outbound_enabled else "معطل", "يحتاج تفويضًا صريحًا"),
        (
            "جلب صفحات الويب لفحص المصادر",
            "مفعّل" if settings.source_fetch_enabled else "معطل",
            "SOURCE_FETCH_ENABLED",
        ),
    ]
    return render(
        request,
        "settings.html",
        members=members,
        workers=workers,
        failed_jobs=failed_jobs,
        integrations=integrations,
        workspace=await db.get(Workspace, p.workspace_id),
        now=datetime.now(UTC),
    )
