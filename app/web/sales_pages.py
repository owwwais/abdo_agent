"""صفحات المبيعات: الموافقات، الفرص، المحادثات، التشغيل والسجلات، والتحكم (إيقاف وتشغيل يدوي)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.api.deps import current_principal, get_db, get_settings_dep
from app.api.errors import AppError, InvalidInput, NotFound
from app.api.serialize import mask
from app.auth.sessions import Principal
from app.config import Settings
from app.db.models import Company, Contact, Job, Product, Segment, Source
from app.db.models_ops import Run, RunEvent
from app.db.models_sales import (
    Approval,
    Conversation,
    Draft,
    Evidence,
    Message,
    Opportunity,
    OutboundCommand,
)
from app.jobs import queue
from app.services import approvals as appr
from app.services import audit
from app.services import integrations as integ
from app.services.common import get_scoped, require_owner
from app.services.inbound import link_message
from app.services.policy import check_draft
from app.web.render import render
from app.workflows.common import local_today, workspace_tz

router = APIRouter(include_in_schema=False)

OPP_LABELS = {
    "discovered": "مكتشفة",
    "qualified": "مؤهلة",
    "draft_ready": "مسودة جاهزة",
    "contacted": "تم التواصل",
    "interested": "مهتمة",
    "meeting_proposed": "اقتراح موعد",
    "not_interested": "غير مهتمة",
    "won": "ربح",
    "lost": "خسارة",
    "archived": "مؤرشفة",
    "disqualified": "غير مؤهلة",
}
_MSG = {
    "approve": "اعتُمدت الرسالة؛ أمر الإرسال في الطابور.",
    "reject": "رُفضت المسودة.",
    "defer": "أُجلت المسودة.",
    "edited": "حُفظ إصدار جديد من المسودة؛ الاعتماد السابق أُبطل.",
    "eligibility": "حُفظت أهلية التواصل.",
    "manual": "سُجل التواصل اليدوي.",
    "reconciled": "سُجلت نتيجة المصالحة.",
    "linked": "رُبطت الرسالة بالفرصة.",
    "queued": "وُضعت المهمة في الطابور؛ تعمل عندما يكون العامل مشغلًا.",
    "status": "تغيرت حالة الفرصة.",
    "control": "حُفظت إعدادات الإيقاف.",
}


def _s(form: FormData, key: str) -> str:
    v = form.get(key)
    return v.strip() if isinstance(v, str) else ""


def _uuid(form: FormData, key: str) -> uuid.UUID:
    try:
        return uuid.UUID(_s(form, key))
    except ValueError as exc:
        raise InvalidInput("معرف غير صالح") from exc


def _back(url: str, ok: str, extra: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(
        f"{url}{sep}ok={ok}" + (f"&msg={quote(extra[:300])}" if extra else ""), status_code=303
    )


def _flash(request: Request) -> str | None:
    base = _MSG.get(request.query_params.get("ok") or "")
    msg = request.query_params.get("msg")
    return f"{base} {msg}" if base and msg else base


def _err(exc: AppError) -> dict[str, Any]:
    return {"message": exc.message, "fields": (exc.details or {}).get("fields", {})}


# ---------------------------------------------------------------- الموافقات


async def _draft_rows(
    db: AsyncSession, p: Principal, statuses: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(Draft, Opportunity, Company, Product)
                .join(Opportunity, Opportunity.id == Draft.opportunity_id)
                .join(Company, Company.id == Opportunity.company_id)
                .join(Product, Product.id == Opportunity.product_id)
                .where(Draft.workspace_id == p.workspace_id, Draft.status.in_(statuses))
                .order_by(Draft.created_at.desc())
                .limit(50)
            )
        )
        .tuples()
        .all()
    )
    out = []
    for draft, opp, company, product in rows:
        contacts = list(
            (await db.execute(select(Contact).where(Contact.company_id == company.id))).scalars()
        )
        evidence = {
            str(e.id): e
            for e in (
                await db.execute(select(Evidence).where(Evidence.company_id == company.id))
            ).scalars()
        }
        cmd = (
            await db.execute(
                select(OutboundCommand)
                .where(OutboundCommand.draft_id == draft.id)
                .order_by(OutboundCommand.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        out.append(
            {
                "draft": draft,
                "opp": opp,
                "company": company,
                "product": product,
                "command": cmd,
                "eligibility": await appr.eligibility(db, draft.recipient_contact_id),
                "contacts": [
                    (c, mask(c.channel, c.value), await appr.eligibility(db, c.id))
                    for c in contacts
                ],
                "facts": [(f, evidence.get(f["evidence_id"])) for f in opp.facts],
                "blocking": [f for f in draft.policy_findings if f.get("blocking")],
                "warnings": [f for f in draft.policy_findings if not f.get("blocking")],
            }
        )
    return out


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    status_code: int = 200,
    error: dict[str, Any] | None = None,
) -> Response:
    pending = await _draft_rows(db, p, ("pending_review", "deferred"))
    approved = await _draft_rows(db, p, ("approved",))
    unknown = list(
        (
            await db.execute(
                select(OutboundCommand, Draft)
                .join(Draft, Draft.id == OutboundCommand.draft_id)
                .where(
                    OutboundCommand.workspace_id == p.workspace_id,
                    OutboundCommand.status == "unknown_delivery",
                )
            )
        ).tuples()
    )
    return render(
        request,
        "approvals.html",
        status_code=status_code,
        pending=pending,
        approved=approved,
        unknown=unknown,
        flash=_flash(request),
        error=error,
        outbound_enabled=settings.outbound_enabled,
        forced_fake=integ.forced_fake(settings),
    )


async def _approvals_error(
    request: Request, p: Principal, db: AsyncSession, settings: Settings, exc: AppError
) -> Response:
    await db.rollback()
    return await approvals_page(
        request, p, db, settings, status_code=exc.status_code, error=_err(exc)
    )


@router.post("/approvals/{draft_id}/decision")
async def decide(
    draft_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    decision = _s(form, "decision")
    try:
        if decision not in ("approve", "reject", "defer"):
            raise InvalidInput("قرار غير معروف")
        revision = int(_s(form, "revision") or 0)
        await appr.decide(
            db,
            settings,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            auth_user_id=p.auth_user_id,
            draft_id=draft_id,
            revision=revision,
            content_hash=_s(form, "content_hash"),
            decision=cast(Any, decision),
            via="web",
            note=_s(form, "note"),
            defer_hours=int(_s(form, "defer_hours") or 24),
        )
        await db.commit()
    except AppError as exc:
        return await _approvals_error(request, p, db, settings, exc)
    return _back("/approvals", decision)


@router.post("/approvals/{draft_id}/edit")
async def edit(
    draft_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        draft = await get_scoped(db, Draft, p.workspace_id, draft_id)
        opp = await db.get(Opportunity, draft.opportunity_id)
        product = await db.get(Product, opp.product_id) if opp else None
        mail = await integ.get_config(db, settings, p.workspace_id, "mail", integ.MailConfig)
        subject, body = _s(form, "subject"), _s(form, "body")
        evidence = (
            [
                e.claim
                for e in (
                    await db.execute(select(Evidence).where(Evidence.company_id == opp.company_id))
                ).scalars()
            ]
            if opp
            else []
        )
        findings = (
            [
                f.as_dict()
                for f in check_draft(
                    subject=subject,
                    body=body,
                    product=product,
                    capabilities_used=[],
                    support_texts=[*evidence, mail.opt_out_line, mail.signature],
                    allowed_urls=[
                        product.product_url or "",
                        product.demo_url or "",
                        mail.booking_link,
                    ],
                )
            ]
            if product
            else []
        )
        contact = _s(form, "contact_id")
        await appr.edit_draft(
            db,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            draft_id=draft_id,
            revision=int(_s(form, "revision") or 0),
            subject=subject,
            body=body,
            contact_id=uuid.UUID(contact) if contact else None,
            findings=findings,
        )
        await db.commit()
    except AppError as exc:
        return await _approvals_error(request, p, db, settings, exc)
    return _back("/approvals", "edited")


@router.post("/contacts/{contact_id}/eligibility")
async def eligibility(
    contact_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        status = _s(form, "status")
        if status not in ("allowed", "disallowed", "unknown"):
            raise InvalidInput("حالة غير معروفة")
        await appr.set_eligibility(
            db,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            contact_id=contact_id,
            status=cast(Any, status),
            basis=_s(form, "basis"),
        )
        await db.commit()
    except AppError as exc:
        return await _approvals_error(request, p, db, settings, exc)
    back = _s(form, "back")
    return _back(
        back if back.startswith("/") and not back.startswith("//") else "/approvals", "eligibility"
    )


@router.post("/approvals/{draft_id}/manual-contact")
async def manual_contact(
    draft_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        await appr.record_manual_contact(
            db,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            draft_id=draft_id,
            note=_s(form, "note"),
        )
        await db.commit()
    except AppError as exc:
        return await _approvals_error(request, p, db, settings, exc)
    return _back("/approvals", "manual")


@router.post("/outbound/{command_id}/reconcile")
async def reconcile(
    command_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    form = await request.form()
    try:
        outcome = _s(form, "outcome")
        if outcome not in ("sent", "not_sent"):
            raise InvalidInput("نتيجة غير معروفة")
        await appr.resolve_unknown_delivery(
            db,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            command_id=command_id,
            outcome=cast(Any, outcome),
        )
        await db.commit()
    except AppError as exc:
        return await _approvals_error(request, p, db, settings, exc)
    return _back("/approvals", "reconciled")


# ---------------------------------------------------------------- الفرص


@router.get("/opportunities", response_class=HTMLResponse)
async def opportunities(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    status = request.query_params.get("status") or None
    raw_page = request.query_params.get("page", "1")
    page = max(1, int(raw_page)) if raw_page.isdigit() else 1
    q = (
        select(Opportunity, Company, Product)
        .join(Company, Company.id == Opportunity.company_id)
        .join(Product, Product.id == Opportunity.product_id)
        .where(Opportunity.workspace_id == p.workspace_id)
    )
    if status in OPP_LABELS:
        q = q.where(Opportunity.status == status)
    total = int((await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one())
    rows = (
        (
            await db.execute(
                q.order_by(Opportunity.updated_at.desc()).limit(50).offset((page - 1) * 50)
            )
        )
        .tuples()
        .all()
    )
    counts = dict(
        (
            await db.execute(
                select(Opportunity.status, func.count())
                .where(Opportunity.workspace_id == p.workspace_id)
                .group_by(Opportunity.status)
            )
        )
        .tuples()
        .all()
    )
    return render(
        request,
        "opportunities.html",
        rows=rows,
        total=total,
        page=page,
        status=status,
        labels=OPP_LABELS,
        counts=counts,
        flash=_flash(request),
    )


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
async def opportunity_detail(
    opp_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    status_code: int = 200,
) -> Response:
    opp = await get_scoped(db, Opportunity, p.workspace_id, opp_id)
    company = await db.get(Company, opp.company_id)
    product = await db.get(Product, opp.product_id)
    segment = await db.get(Segment, opp.segment_id) if opp.segment_id else None
    source = await db.get(Source, opp.source_id) if opp.source_id else None
    evidence = list(
        (
            await db.execute(
                select(Evidence)
                .where(Evidence.company_id == opp.company_id)
                .order_by(Evidence.observed_at.desc())
            )
        ).scalars()
    )
    drafts = list(
        (
            await db.execute(
                select(Draft)
                .where(Draft.opportunity_id == opp.id)
                .order_by(Draft.created_at.desc())
            )
        ).scalars()
    )
    approvals_ = list(
        (
            await db.execute(
                select(Approval)
                .where(Approval.draft_id.in_([d.id for d in drafts] or [uuid.UUID(int=0)]))
                .order_by(Approval.decided_at.desc())
            )
        ).scalars()
    )
    convs = list(
        (
            await db.execute(select(Conversation).where(Conversation.opportunity_id == opp.id))
        ).scalars()
    )
    messages = list(
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id.in_([c.id for c in convs] or [uuid.UUID(int=0)]))
                .order_by(Message.created_at)
            )
        ).scalars()
    )
    runs_ = list(
        (
            await db.execute(
                select(Run)
                .where(Run.opportunity_id == opp.id)
                .order_by(Run.started_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    contacts = [
        (c, mask(c.channel, c.value), await appr.eligibility(db, c.id))
        for c in (
            await db.execute(select(Contact).where(Contact.company_id == opp.company_id))
        ).scalars()
    ]
    return render(
        request,
        "opportunity_detail.html",
        status_code=status_code,
        opp=opp,
        company=company,
        product=product,
        segment=segment,
        source=source,
        evidence=evidence,
        drafts=drafts,
        approvals=approvals_,
        messages=messages,
        runs=runs_,
        contacts=contacts,
        labels=OPP_LABELS,
        flash=_flash(request),
        evidence_ids={str(e) for e in opp.evidence_ids},
    )


@router.post("/opportunities/{opp_id}/process")
async def process_now(
    opp_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_owner(p)
    opp = await get_scoped(db, Opportunity, p.workspace_id, opp_id, lock=True)
    if opp.status not in ("discovered", "qualified", "disqualified"):
        raise AppError("حالة الفرصة لا تسمح بالمعالجة الآن", code="invalid_state")
    if opp.status == "disqualified":
        opp.status = "discovered"
    opp.next_action_at = None
    await queue.enqueue(
        db,
        kind="process_window",
        workspace_id=p.workspace_id,
        payload={"opportunity_id": str(opp.id)},
        idempotency_key=f"{p.workspace_id}:process:opp:{opp.id}:{datetime.now(UTC):%Y%m%d%H%M}",
    )
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="opportunity.process_now",
        entity_type="opportunity",
        entity_id=opp.id,
    )
    await db.commit()
    return _back(f"/opportunities/{opp_id}", "queued")


@router.post("/opportunities/{opp_id}/status")
async def set_status(
    opp_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    target = _s(form, "status")
    if target not in ("won", "lost", "archived", "not_interested", "discovered"):
        raise InvalidInput("حالة غير مسموحة يدويًا")
    opp = await get_scoped(db, Opportunity, p.workspace_id, opp_id, lock=True)
    old = opp.status
    opp.status = target
    opp.status_changed_by = p.actor_id
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="opportunity.status",
        entity_type="opportunity",
        entity_id=opp.id,
        change={"from": old, "to": target},
    )
    await db.commit()
    return _back(f"/opportunities/{opp_id}", "status")


# ---------------------------------------------------------------- المحادثات


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    status_code: int = 200,
    error: dict[str, Any] | None = None,
) -> Response:
    needs = list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.workspace_id == p.workspace_id, Message.link_status == "needs_linking"
                )
                .order_by(Message.created_at.desc())
            )
        ).scalars()
    )
    candidates: dict[str, list[tuple[Opportunity, Company, Product]]] = {}
    for m in needs:
        ids = [uuid.UUID(x) for x in m.candidate_opportunity_ids]
        if m.company_id:
            ids += list(
                (
                    await db.execute(
                        select(Opportunity.id).where(Opportunity.company_id == m.company_id)
                    )
                ).scalars()
            )
        rows = (
            (
                await db.execute(
                    select(Opportunity, Company, Product)
                    .join(Company, Company.id == Opportunity.company_id)
                    .join(Product, Product.id == Opportunity.product_id)
                    .where(
                        Opportunity.id.in_(ids or [uuid.UUID(int=0)]),
                        Opportunity.workspace_id == p.workspace_id,
                    )
                )
            )
            .tuples()
            .all()
        )
        candidates[str(m.id)] = list(rows)
    recent = list(
        (
            await db.execute(
                select(Message, Company)
                .join(Company, Company.id == Message.company_id, isouter=True)
                .where(Message.workspace_id == p.workspace_id)
                .order_by(Message.created_at.desc())
                .limit(40)
            )
        ).tuples()
    )
    return render(
        request,
        "conversations.html",
        status_code=status_code,
        needs=needs,
        candidates=candidates,
        recent=recent,
        flash=_flash(request),
        error=error,
    )


@router.post("/messages/{message_id}/link")
async def link(
    message_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    try:
        await link_message(
            db,
            workspace_id=p.workspace_id,
            actor_id=p.actor_id,
            message_id=message_id,
            opportunity_id=_uuid(form, "opportunity_id"),
        )
        await db.commit()
    except AppError as exc:
        await db.rollback()
        return await conversations(request, p, db, status_code=exc.status_code, error=_err(exc))
    return _back("/conversations", "linked")


# ---------------------------------------------------------------- التشغيل والتحكم


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(
    request: Request, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> Response:
    rows = list(
        (
            await db.execute(
                select(Run)
                .where(Run.workspace_id == p.workspace_id)
                .order_by(Run.started_at.desc())
                .limit(40)
            )
        ).scalars()
    )
    selected = request.query_params.get("run")
    events: list[RunEvent] = []
    if selected:
        try:
            run = await get_scoped(db, Run, p.workspace_id, uuid.UUID(selected))
            events = list(
                (
                    await db.execute(
                        select(RunEvent)
                        .where(RunEvent.run_id == run.id)
                        .order_by(RunEvent.occurred_at)
                    )
                ).scalars()
            )
        except (ValueError, NotFound):
            events = []
    jobs = list(
        (
            await db.execute(
                select(Job)
                .where(Job.workspace_id == p.workspace_id)
                .order_by(Job.created_at.desc())
                .limit(25)
            )
        ).scalars()
    )
    return render(
        request,
        "runs.html",
        runs=rows,
        events=events,
        selected=selected,
        jobs=jobs,
        flash=_flash(request),
    )


@router.post("/runs/start")
async def start_run(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    kind = _s(form, "kind")
    tz = await workspace_tz(request.app.state.sessionmaker, p.workspace_id, settings.app_timezone)
    today = local_today(tz)
    keys = {
        "discover": ("discover_daily", f"{p.workspace_id}:discover:{today}"),
        "process": (
            "process_window",
            f"{p.workspace_id}:process:manual:{datetime.now(UTC):%Y%m%d%H%M}",
        ),
        "mail_sync": (
            "mail_sync",
            f"{p.workspace_id}:mailsync:manual:{datetime.now(UTC):%Y%m%d%H%M}",
        ),
        "digest": ("digest", f"{p.workspace_id}:digest:{today}"),
    }
    if kind not in keys:
        raise InvalidInput("نوع تشغيل غير معروف")
    job_kind, key = keys[kind]
    await queue.enqueue(
        db,
        kind=job_kind,
        workspace_id=p.workspace_id,
        payload={"manual": True},
        idempotency_key=key,
    )
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action=f"run.manual.{kind}",
        entity_type="job",
    )
    await db.commit()
    return _back("/runs", "queued")


@router.post("/control")
async def control(
    request: Request,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    require_owner(p)
    form = await request.form()
    ops = await integ.get_config(db, settings, p.workspace_id, "operations", integ.OperationsConfig)
    updated = ops.model_copy(
        update={
            "pause_discovery": _s(form, "pause_discovery") == "on",
            "pause_outbound": _s(form, "pause_outbound") == "on",
            "pause_all_processing": _s(form, "pause_all_processing") == "on",
        }
    )
    await integ.save_config(
        db,
        p.workspace_id,
        "operations",
        updated,
        version=await integ.get_version(db, p.workspace_id, "operations"),
        actor_id=p.actor_id,
    )
    await db.commit()
    return _back("/", "control")
