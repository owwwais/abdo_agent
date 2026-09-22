"""واجهة JSON للمبيعات (SPEC §12): الفرص، المسودات، القرارات، الربط اليدوي، التواصل اليدوي، التحكم، التشغيلات.

القرار idempotent لكل (مسودة، إصدار): تكرار الطلب نفسه يعيد القرار المسجل دون أمر إرسال ثانٍ.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, get_db, get_settings_dep
from app.api.serialize import mask
from app.auth.sessions import Principal
from app.config import Settings
from app.db.models import Company, Contact, Product
from app.db.models_ops import Run, RunEvent, UsageLedger
from app.db.models_sales import Conversation, Draft, Evidence, Message, Opportunity
from app.services import approvals as appr
from app.services import integrations as integ
from app.services.common import get_scoped, require_owner
from app.services.inbound import link_message

router = APIRouter(prefix="/api")


def _iso(v: Any) -> Any:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def opportunity_out(
    o: Opportunity, company: Company | None = None, product: Product | None = None
) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "status": o.status,
        "score": o.score,
        "preliminary_score": o.preliminary_score,
        "score_breakdown": o.score_breakdown,
        "facts": o.facts,
        "hypotheses": o.hypotheses,
        "missing_info": o.missing_info,
        "selection_reason": o.selection_reason,
        "disqualify_reason": o.disqualify_reason,
        "company_id": str(o.company_id),
        "product_id": str(o.product_id),
        "company_name": company.display_name if company else None,
        "product_name": product.name if product else None,
        "is_demo_data": o.is_demo_data,
        "created_at": _iso(o.created_at),
        "updated_at": _iso(o.updated_at),
    }


def draft_out(d: Draft) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "opportunity_id": str(d.opportunity_id),
        "kind": d.kind,
        "channel": d.channel,
        "revision": d.revision,
        "status": d.status,
        "subject": d.subject,
        "body": d.body,
        "recipient": d.recipient_snapshot,
        "content_hash": d.content_hash,
        "product_version": d.product_version,
        "policy_findings": d.policy_findings,
        "defer_until": _iso(d.defer_until),
        "updated_at": _iso(d.updated_at),
    }


@router.get("/opportunities")
async def list_opportunities(
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None, max_length=20),
    product_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    q = (
        select(Opportunity, Company, Product)
        .join(Company, Company.id == Opportunity.company_id)
        .join(Product, Product.id == Opportunity.product_id)
        .where(Opportunity.workspace_id == p.workspace_id)
    )
    if status:
        q = q.where(Opportunity.status == status)
    if product_id:
        q = q.where(Opportunity.product_id == product_id)
    total = int((await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one())
    rows = (
        await db.execute(
            q.order_by(Opportunity.updated_at.desc()).limit(per_page).offset((page - 1) * per_page)
        )
    ).tuples()
    return {
        "items": [opportunity_out(o, c, pr) for o, c, pr in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/opportunities/{opp_id}")
async def get_opportunity(
    opp_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    o = await get_scoped(db, Opportunity, p.workspace_id, opp_id)
    company, product = await db.get(Company, o.company_id), await db.get(Product, o.product_id)
    evidence = (
        await db.execute(select(Evidence).where(Evidence.company_id == o.company_id))
    ).scalars()
    drafts = (
        await db.execute(
            select(Draft).where(Draft.opportunity_id == o.id).order_by(Draft.created_at.desc())
        )
    ).scalars()
    conv_ids = [
        c
        for c in (
            await db.execute(select(Conversation.id).where(Conversation.opportunity_id == o.id))
        ).scalars()
    ]
    messages = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id.in_(conv_ids or [uuid.UUID(int=0)]))
            .order_by(Message.created_at)
        )
    ).scalars()
    contacts = (
        await db.execute(select(Contact).where(Contact.company_id == o.company_id))
    ).scalars()
    return {
        **opportunity_out(o, company, product),
        "evidence": [
            {
                "id": str(e.id),
                "claim": e.claim,
                "fact_type": e.fact_type,
                "url": e.url,
                "observed_at": _iso(e.observed_at),
                "expires_at": _iso(e.expires_at),
            }
            for e in evidence
        ],
        "drafts": [draft_out(d) for d in drafts],
        "messages": [
            {
                "id": str(m.id),
                "direction": m.direction,
                "channel": m.channel,
                "subject": m.subject,
                "body_text": m.body_text,
                "classification": m.classification,
                "at": _iso(m.sent_or_received_at),
            }
            for m in messages
        ],
        "contacts": [
            {
                "id": str(c.id),
                "channel": c.channel,
                "value_masked": mask(c.channel, c.value),
                "eligibility": await appr.eligibility(db, c.id),
            }
            for c in contacts
        ],
    }


class DraftPatch(BaseModel):
    revision: int = Field(ge=1)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=20000)
    contact_id: uuid.UUID | None = None


@router.patch("/drafts/{draft_id}")
async def patch_draft(
    draft_id: uuid.UUID,
    data: DraftPatch,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    d = await appr.edit_draft(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        draft_id=draft_id,
        revision=data.revision,
        subject=data.subject,
        body=data.body,
        contact_id=data.contact_id,
    )
    await db.commit()
    return draft_out(d)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject", "defer"]
    revision: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    note: str = Field(default="", max_length=1000)
    defer_hours: int = Field(default=24, ge=1, le=720)


@router.post("/approvals/{draft_id}/decision")
async def decision(
    draft_id: uuid.UUID,
    data: DecisionIn,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    approval, created = await appr.decide(
        db,
        settings,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        auth_user_id=p.auth_user_id,
        draft_id=draft_id,
        revision=data.revision,
        content_hash=data.content_hash,
        decision=data.decision,
        via="api",
        note=data.note,
        defer_hours=data.defer_hours,
    )
    await db.commit()
    return {
        "approval_id": str(approval.id),
        "decision": approval.decision,
        "status": approval.status,
        "revision": approval.draft_revision,
        "created": created,
    }


class LinkIn(BaseModel):
    opportunity_id: uuid.UUID


@router.post("/conversations/{message_id}/link")
async def link(
    message_id: uuid.UUID,
    data: LinkIn,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """{message_id} هو الرسالة الواردة التي تنتظر الربط (link_status=needs_linking)."""
    m = await link_message(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        message_id=message_id,
        opportunity_id=data.opportunity_id,
    )
    await db.commit()
    return {
        "message_id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "link_status": m.link_status,
    }


class ManualContactIn(BaseModel):
    draft_id: uuid.UUID
    note: str = Field(default="", max_length=1000)


@router.post("/manual-contacts", status_code=201)
async def manual_contact(
    data: ManualContactIn,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    mc = await appr.record_manual_contact(
        db, workspace_id=p.workspace_id, actor_id=p.actor_id, draft_id=data.draft_id, note=data.note
    )
    await db.commit()
    return {"id": str(mc.id), "opportunity_id": str(mc.opportunity_id), "channel": mc.channel}


class ControlIn(BaseModel):
    pause_discovery: bool | None = None
    pause_outbound: bool | None = None
    pause_all_processing: bool | None = None


@router.post("/control")
async def control(
    data: ControlIn,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    require_owner(p)
    ops = await integ.get_config(db, settings, p.workspace_id, "operations", integ.OperationsConfig)
    updated = ops.model_copy(update=data.model_dump(exclude_none=True))
    await integ.save_config(
        db,
        p.workspace_id,
        "operations",
        updated,
        version=await integ.get_version(db, p.workspace_id, "operations"),
        actor_id=p.actor_id,
    )
    await db.commit()
    return {
        k: getattr(updated, k)
        for k in ("pause_discovery", "pause_outbound", "pause_all_processing")
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID, p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    r = await get_scoped(db, Run, p.workspace_id, run_id)
    events = (
        await db.execute(
            select(RunEvent).where(RunEvent.run_id == r.id).order_by(RunEvent.occurred_at)
        )
    ).scalars()
    cost = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(func.coalesce(UsageLedger.actual_cost, UsageLedger.estimated_cost)), 0
                )
            ).where(UsageLedger.run_id == r.id)
        )
    ).scalar_one()
    return {
        "id": str(r.id),
        "kind": r.kind,
        "status": r.status,
        "summary": r.summary,
        "error_code": r.error_code,
        "opportunity_id": str(r.opportunity_id) if r.opportunity_id else None,
        "started_at": _iso(r.started_at),
        "ended_at": _iso(r.ended_at),
        "cost": str(cost),
        "events": [
            {
                "step": e.step,
                "type": e.event_type,
                "summary": e.sanitized_summary,
                "at": _iso(e.occurred_at),
            }
            for e in events
        ],
    }


@router.post("/products/{product_id}/test")
async def product_test(
    product_id: uuid.UUID,
    request: Request,
    p: Principal = Depends(current_principal),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """اختبار فهم محدود للمنتج بالنموذج المختار (فئة ميزانية test)."""
    from app.services.connection_tests import test_product_understanding

    require_owner(p)
    return await test_product_understanding(
        settings, request.app.state.sessionmaker, p.workspace_id, product_id, p.actor_id
    )
