"""صفحة الإعدادات والتكاملات: التشغيل والميزانية، النماذج، البريد، تيليجرام، البحث، الأعضاء والنظام.

حقول الأسرار للكتابة فقط: الحقل الفارغ يُبقي القيمة الحالية، ولا تُعرض أي قيمة محفوظة أبدًا.
"""

from __future__ import annotations

import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import FormData

from app.api.deps import current_principal, get_db, get_settings_dep
from app.api.errors import AppError, InvalidInput
from app.auth.sessions import Principal
from app.config import Settings
from app.db.models import Job, Membership, UserProfile, WorkerHeartbeat, Workspace
from app.db.models_sales import TelegramLinkCode
from app.services import budget
from app.services import connection_tests as ct
from app.services import integrations as integ
from app.services import secrets as sec
from app.services.channels import telegram_context
from app.services.common import require_owner, validation_details
from app.web.render import render

router = APIRouter(include_in_schema=False)
TABS = {
    "operations": "التشغيل والميزانية",
    "models": "النماذج",
    "mail": "البريد",
    "telegram": "تيليجرام",
    "search": "البحث",
    "members": "الأعضاء والنظام",
}
_MSG = {
    "saved": "حُفظت الإعدادات.",
    "tested": "نجح الاختبار.",
    "webhook_set": "رُبط ويب هوك تيليجرام.",
    "webhook_deleted": "أُلغي ويب هوك تيليجرام؛ الوضع الآن polling.",
}


def _s(form: FormData, key: str) -> str:
    v = form.get(key)
    return v.strip() if isinstance(v, str) else ""


def _b(form: FormData, key: str) -> bool:
    return _s(form, key) == "on"


def _dec(form: FormData, key: str) -> Decimal | None:
    raw = _s(form, key).replace("٫", ".").replace(",", ".")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidInput("رقم غير صالح", details={"fields": {key: "رقم غير صالح"}}) from exc


def _int(form: FormData, key: str, default: int) -> int:
    raw = _s(form, key)
    return int(raw) if raw.isdigit() else default


async def _apply_secrets(
    db: AsyncSession, settings: Settings, p: Principal, form: FormData, keys: list[str]
) -> None:
    for key in keys:
        if _b(form, f"delete_{key}"):
            await sec.delete_secret(db, p.workspace_id, key, p.actor_id)
        value = _s(form, f"secret_{key}")
        if value:
            await sec.set_secret(db, settings, p.workspace_id, key, value, p.actor_id)


async def _page(
    request: Request,
    p: Principal,
    db: AsyncSession,
    settings: Settings,
    tab: str,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tab = tab if tab in TABS else "operations"
    ws = p.workspace_id
    ctx: dict[str, Any] = {
        "tab": tab,
        "tabs": TABS,
        "secret_status": await sec.secret_status(db, settings, ws),
        "secret_specs": sec.SECRETS,
        "health": await integ.get_health(db, ws),
        "forced_fake": integ.forced_fake(settings),
        "base_url": settings.app_base_url.rstrip("/"),
        "outbound_enabled": settings.outbound_enabled,
        "app_env": settings.app_env.value,
        "tested_msg": request.query_params.get("msg"),
        "ok_msg": _MSG.get(request.query_params.get("ok") or ""),
    }
    for type_, model in integ.CONFIG_TYPES.items():
        ctx[f"cfg_{type_}"] = await integ.get_config(db, settings, ws, type_, model)
        ctx[f"ver_{type_}"] = await integ.get_version(db, ws, type_)
    ctx["readiness"] = await ct.live_readiness(db, settings, ws)
    ctx["suggested_prices"] = integ.SUGGESTED_PRICES
    workspace = await db.get(Workspace, ws)
    tz = workspace.timezone if workspace else settings.app_timezone
    ctx["usage"] = await budget.usage_summary(db, ws, tz)
    if tab == "members":
        ctx["members"] = list(
            (
                await db.execute(
                    select(Membership, UserProfile)
                    .join(UserProfile, UserProfile.auth_user_id == Membership.auth_user_id)
                    .where(Membership.workspace_id == ws)
                    .order_by(Membership.role, UserProfile.display_name)
                )
            ).tuples()
        )
        ctx["workers"] = list(
            (
                await db.execute(
                    select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(5)
                )
            ).scalars()
        )
        ctx["failed_jobs"] = list(
            (
                await db.execute(
                    select(Job)
                    .where(Job.workspace_id == ws, Job.status == "failed")
                    .order_by(Job.updated_at.desc())
                    .limit(10)
                )
            ).scalars()
        )
        ctx["now"] = datetime.now(UTC)
        me = await db.get(UserProfile, p.auth_user_id)
        ctx["my_telegram_id"] = me.telegram_user_id if me else None
    ctx.update(extra)
    return render(request, "settings.html", status_code=status_code, workspace=workspace, **ctx)


def _err(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        return {
            "message": "راجع الحقول المحددة",
            "fields": validation_details(exc).get("fields", {}),
        }
    if isinstance(exc, AppError):
        return {"message": exc.message, "fields": (exc.details or {}).get("fields", {})}
    raise exc


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    return await _page(request, p, db, settings, request.query_params.get("tab", "operations"))


def _done(tab: str, ok: str = "saved", msg: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    url = f"/settings?tab={tab}&ok={ok}"
    if msg:
        url += "&msg=" + quote(msg[:400])
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- التشغيل والميزانية


@router.post("/settings/operations")
async def save_operations(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        cfg = integ.OperationsConfig(
            operating_mode=_s(form, "operating_mode") or "demo",
            daily_budget=_dec(form, "daily_budget"),
            monthly_budget=_dec(form, "monthly_budget"),
            currency=(_s(form, "currency") or "USD").upper(),
            discovery_daily_cap=_dec(form, "discovery_daily_cap"),
            new_opportunity_daily_cap=_dec(form, "new_opportunity_daily_cap"),
            followup_daily_cap=_dec(form, "followup_daily_cap"),
            max_qualified_per_day=_int(form, "max_qualified_per_day", 3),
            discovery_time=_s(form, "discovery_time") or "09:00",
            process_slots=[
                x for x in _s(form, "process_slots").replace("،", ",").split(",") if x.strip()
            ]
            or ["10:00", "12:00", "14:00"],
            digest_time=_s(form, "digest_time") or "18:00",
            grace_hours=_int(form, "grace_hours", 3),
            score_threshold=_int(form, "score_threshold", 70),
            first_contact_cooldown_days=_int(form, "first_contact_cooldown_days", 14),
            approval_expiry_hours=_int(form, "approval_expiry_hours", 48),
            max_model_calls_per_opportunity=_int(form, "max_model_calls_per_opportunity", 5),
            max_enrichment_pages=_int(form, "max_enrichment_pages", 3),
            pause_discovery=_b(form, "pause_discovery"),
            pause_outbound=_b(form, "pause_outbound"),
            pause_all_processing=_b(form, "pause_all_processing"),
        )
        if cfg.operating_mode == "live":
            # نحفظ أولًا بالقيم الجديدة ثم نتحقق من الجاهزية بها (الميزانية قد تكون أُدخلت الآن).
            await integ.save_config(
                db,
                p.workspace_id,
                "operations",
                cfg,
                version=_int(form, "version", 0),
                actor_id=p.actor_id,
            )
            readiness = await ct.live_readiness(db, settings, p.workspace_id)
            if readiness["blockers"]:
                raise InvalidInput(
                    "لا يمكن التحويل إلى التشغيل الفعلي: " + "؛ ".join(readiness["blockers"]),
                    code="live_not_ready",
                )
        else:
            await integ.save_config(
                db,
                p.workspace_id,
                "operations",
                cfg,
                version=_int(form, "version", 0),
                actor_id=p.actor_id,
            )
        ws = await db.get(Workspace, p.workspace_id)
        if ws is not None:
            ws.operating_mode = cfg.operating_mode
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _page(
            request,
            p,
            db,
            settings,
            "operations",
            422 if not isinstance(exc, AppError) else exc.status_code,
            error=_err(exc),
        )
    return _done("operations")


# ---------------------------------------------------------------- النماذج


@router.post("/settings/models")
async def save_models(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        current = await integ.get_config(db, settings, p.workspace_id, "models", integ.ModelsConfig)
        roles = {}
        for role in ("extractor", "writer"):
            roles[role] = integ.ModelRole(
                provider=_s(form, f"{role}_provider") or "fake", model=_s(form, f"{role}_model")
            )
        prices = dict(current.prices)
        for role in ("extractor", "writer"):
            r = roles[role]
            if r.provider == "fake" or not r.model:
                continue
            key = f"{r.provider}:{r.model}"
            inp, out = _dec(form, f"{role}_price_in"), _dec(form, f"{role}_price_out")
            if inp is not None and out is not None:
                prices[key] = integ.ModelPrice(
                    input_per_mtok=inp,
                    output_per_mtok=out,
                    currency=(_s(form, "price_currency") or "USD").upper(),
                    pricing_version=f"owner-{datetime.now(UTC).date().isoformat()}",
                )
        cfg = integ.ModelsConfig(
            extractor=roles["extractor"],
            writer=roles["writer"],
            openai_compatible_base_url=_s(form, "openai_compatible_base_url"),
            openai_compatible_name=_s(form, "openai_compatible_name"),
            prices=prices,
            max_output_tokens=_int(form, "max_output_tokens", 4000),
            processing_approved=_b(form, "processing_approved"),
            processing_note=_s(form, "processing_note"),
        )
        await _apply_secrets(
            db,
            settings,
            p,
            form,
            ["anthropic_api_key", "openai_api_key", "gemini_api_key", "openai_compatible_api_key"],
        )
        await integ.save_config(
            db, p.workspace_id, "models", cfg, version=_int(form, "version", 0), actor_id=p.actor_id
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _page(request, p, db, settings, "models", 422, error=_err(exc))
    return _done("models")


@router.post("/settings/models/fetch")
async def fetch_models(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        models = await ct.fetch_models(db, settings, p.workspace_id, _s(form, "provider"))
        await db.commit()
    except AppError as exc:
        await db.commit()  # نحفظ نتيجة الاختبار الفاشلة في health
        return await _page(request, p, db, settings, "models", exc.status_code, error=_err(exc))
    return _done(
        "models", "tested", f"{_s(form, 'provider')}: {len(models)} نموذج متاح؛ اختره من القائمة"
    )


@router.post("/settings/models/test")
async def test_model(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    try:
        msg = await ct.test_model_role(settings, sm, p.workspace_id, _s(form, "role"))
    except AppError as exc:
        return await _page(request, p, db, settings, "models", exc.status_code, error=_err(exc))
    return _done("models", "tested", msg)


# ---------------------------------------------------------------- البريد


@router.post("/settings/mail")
async def save_mail(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        preset = _s(form, "preset") or "hostinger"
        values: dict[str, Any] = {
            "provider": _s(form, "provider") or "fake",
            "preset": preset,
            "smtp_username": _s(form, "smtp_username"),
            "from_address": _s(form, "from_address"),
            "from_name": _s(form, "from_name"),
            "reply_to": _s(form, "reply_to"),
            "imap_enabled": _b(form, "imap_enabled"),
            "imap_same_password": _b(form, "imap_same_password"),
            "imap_folder": _s(form, "imap_folder") or "INBOX",
            "sent_folder": _s(form, "sent_folder"),
            "imap_username": _s(form, "imap_username"),
            "inbound_mode": _s(form, "inbound_mode") or "webhook_and_imap",
            "sync_interval_minutes": _int(form, "sync_interval_minutes", 10),
            "daily_send_limit": _int(form, "daily_send_limit", 20),
            "signature": _s(form, "signature"),
            "booking_link": _s(form, "booking_link"),
        }
        if _s(form, "opt_out_line"):
            values["opt_out_line"] = _s(form, "opt_out_line")
        if preset == "hostinger":
            # ربط تلقائي لبريد Hostinger: SMTP 465 SSL وIMAP 993 SSL.
            values.update(
                smtp_host="smtp.hostinger.com",
                smtp_port=465,
                smtp_security="ssl",
                imap_host="imap.hostinger.com",
                imap_port=993,
            )
        else:
            values.update(
                smtp_host=_s(form, "smtp_host"),
                smtp_port=_int(form, "smtp_port", 465),
                smtp_security=_s(form, "smtp_security") or "ssl",
                imap_host=_s(form, "imap_host"),
                imap_port=_int(form, "imap_port", 993),
            )
        cfg = integ.MailConfig(**values)
        await _apply_secrets(
            db, settings, p, form, ["smtp_password", "imap_password", "hostinger_webhook_secret"]
        )
        await integ.save_config(
            db, p.workspace_id, "mail", cfg, version=_int(form, "version", 0), actor_id=p.actor_id
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _page(request, p, db, settings, "mail", 422, error=_err(exc))
    return _done("mail")


@router.post("/settings/mail/test")
async def test_mail(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        if _s(form, "action") == "send":
            msg = await ct.send_test_mail(db, settings, p.workspace_id, p.email)
        else:
            msg = await ct.test_mail(db, settings, p.workspace_id)
        await db.commit()
    except AppError as exc:
        await db.commit()
        return await _page(request, p, db, settings, "mail", exc.status_code, error=_err(exc))
    return _done("mail", "tested", msg)


# ---------------------------------------------------------------- تيليجرام


@router.post("/settings/telegram")
async def save_telegram(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        current = await integ.get_config(
            db, settings, p.workspace_id, "telegram", integ.TelegramConfig
        )
        cfg = integ.TelegramConfig(
            enabled=_b(form, "enabled"),
            chat_id=_s(form, "chat_id"),
            mode=current.mode,
            webhook_url=current.webhook_url,
        )
        await _apply_secrets(db, settings, p, form, ["telegram_bot_token"])
        await integ.save_config(
            db,
            p.workspace_id,
            "telegram",
            cfg,
            version=_int(form, "version", 0),
            actor_id=p.actor_id,
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _page(request, p, db, settings, "telegram", 422, error=_err(exc))
    return _done("telegram")


@router.post("/settings/telegram/test")
async def test_telegram(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        msg = await ct.test_telegram(
            db, settings, p.workspace_id, send=_s(form, "action") == "send"
        )
        await db.commit()
    except AppError as exc:
        await db.commit()
        return await _page(request, p, db, settings, "telegram", exc.status_code, error=_err(exc))
    return _done("telegram", "tested", msg)


@router.post("/settings/telegram/webhook")
async def telegram_webhook_mode(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """الويب هوك يحتاج https عامًا؛ محليًا يبقى وضع polling (العامل يسحب التحديثات)."""
    require_owner(p)
    form = await request.form()
    try:
        tg = await telegram_context(db, settings, p.workspace_id)
        if tg.client is None:
            raise AppError("فعّل تيليجرام واحفظ رمز البوت أولًا", code="telegram_not_configured")
        cfg = tg.config
        if _s(form, "action") == "delete":
            await tg.client.delete_webhook()
            cfg = cfg.model_copy(update={"mode": "polling", "webhook_url": ""})
            ok = "webhook_deleted"
        else:
            if not settings.cookie_secure:
                raise AppError(
                    "الويب هوك يحتاج APP_BASE_URL بـhttps؛ محليًا استخدم وضع polling",
                    code="https_required",
                )
            secret = pysecrets.token_urlsafe(32)
            url = f"{settings.app_base_url.rstrip('/')}/webhooks/telegram"
            await tg.client.set_webhook(url, secret)
            await sec.set_secret(
                db, settings, p.workspace_id, "telegram_webhook_secret", secret, p.actor_id
            )
            cfg = cfg.model_copy(update={"mode": "webhook", "webhook_url": url})
            ok = "webhook_set"
        version = await integ.get_version(db, p.workspace_id, "telegram")
        await integ.save_config(
            db, p.workspace_id, "telegram", cfg, version=version, actor_id=p.actor_id
        )
        await db.commit()
    except AppError as exc:
        await db.rollback()
        return await _page(request, p, db, settings, "telegram", exc.status_code, error=_err(exc))
    except Exception as exc:
        await db.rollback()
        return await _page(
            request,
            p,
            db,
            settings,
            "telegram",
            502,
            error={"message": str(exc)[:300], "fields": {}},
        )
    return _done("telegram", ok)


@router.post("/settings/telegram/link-code")
async def telegram_link_code(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """رمز لمرة واحدة يربط حساب تيليجرام العضو بعضويته: يرسله للبوت بـ /link CODE."""
    code = f"{pysecrets.randbelow(10**8):08d}"
    db.add(
        TelegramLinkCode(
            code=code,
            workspace_id=p.workspace_id,
            auth_user_id=p.auth_user_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await db.commit()
    return _done("members", "tested", f"أرسل للبوت خلال 15 دقيقة: /link {code}")


# ---------------------------------------------------------------- البحث


@router.post("/settings/search")
async def save_search(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    try:
        cfg = integ.SearchConfig(
            provider=_s(form, "provider") or "fake",
            country=(_s(form, "country") or "SA").upper(),
            search_lang=_s(form, "search_lang") or "ar",
            max_queries=_int(form, "max_queries", 3),
            max_candidates=_int(form, "max_candidates", 20),
            price_per_1k_requests=_dec(form, "price_per_1k_requests"),
            currency=(_s(form, "currency") or "USD").upper(),
        )
        await _apply_secrets(db, settings, p, form, ["brave_api_key"])
        await integ.save_config(
            db, p.workspace_id, "search", cfg, version=_int(form, "version", 0), actor_id=p.actor_id
        )
        await db.commit()
    except (ValidationError, AppError) as exc:
        await db.rollback()
        return await _page(request, p, db, settings, "search", 422, error=_err(exc))
    return _done("search")


@router.post("/settings/search/test")
async def test_search(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    try:
        msg = await ct.test_search(db, settings, p.workspace_id)
        await db.commit()
    except AppError as exc:
        await db.commit()
        return await _page(request, p, db, settings, "search", exc.status_code, error=_err(exc))
    return _done("search", "tested", msg)
