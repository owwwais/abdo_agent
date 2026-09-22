"""بيانات مبيعات اصطناعية للاختبارات: فئة ومنتج وجهة ومسودة جاهزة للمراجعة، دون المرور بالنموذج."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import data_hash
from app.connectors.mail import InboundMail
from app.db.models import Company, Contact, Product, Segment
from app.db.models_sales import Opportunity
from app.jobs import queue
from app.services import approvals
from app.services.outbound import send_outbound
from app.workflows.common import Deps
from tests.conftest import WorkspaceFixture, make_settings


@dataclass
class Seeded:
    product: uuid.UUID
    company: uuid.UUID
    contact: uuid.UUID
    opportunity: uuid.UUID
    draft: uuid.UUID
    value: str


async def seed_draft(
    sm: async_sessionmaker[AsyncSession],
    ws: WorkspaceFixture,
    *,
    channel: str = "email",
    value: str = "buyer@clinic-waha.example",
    allowed: bool = True,
    company_name: str = "مجمع عيادات الواحة",
    domain: str = "clinic-waha.example",
) -> Seeded:
    settings = make_settings()
    async with sm() as db, db.begin():
        seg = Segment(workspace_id=ws.id, name=f"عيادات {uuid.uuid4().hex[:4]}")
        db.add(seg)
        product = Product(
            workspace_id=ws.id,
            name="منظم المواعيد",
            status="active",
            priority=3,
            problem="ضياع الحجوزات",
            summary="صفحة حجز وتذكير",
            capabilities=["صفحة حجز عامة"],
        )
        db.add(product)
        company = Company(
            workspace_id=ws.id,
            display_name=company_name,
            normalized_name=company_name,
            domain=domain,
        )
        db.add(company)
        await db.flush()
        kind = "email" if channel == "email" else "phone"
        contact = Contact(
            workspace_id=ws.id,
            company_id=company.id,
            channel=kind,
            value=value,
            value_hash=data_hash(settings, kind, value.lower()),
            normalized=True,
        )
        db.add(contact)
        opp = Opportunity(
            workspace_id=ws.id,
            company_id=company.id,
            product_id=product.id,
            segment_id=seg.id,
            status="draft_ready",
            score=82,
            facts=[],
        )
        db.add(opp)
        await db.flush()
        draft = await approvals.create_draft(
            db,
            workspace_id=ws.id,
            opportunity=opp,
            kind="first_contact",
            channel=channel,
            contact=contact,
            subject="تنظيم مواعيد العيادة",
            body="السلام عليكم، لاحظنا أن الحجز لديكم عبر الهاتف فقط.\nلإيقاف التواصل ردوا بكلمة إيقاف.",
            product_version=product.version,
            findings=[],
            model_meta={},
            created_by="service:test",
        )
        if allowed:
            await approvals.set_eligibility(
                db,
                workspace_id=ws.id,
                actor_id=f"user:{ws.owner.auth_user_id}",
                contact_id=contact.id,
                status="allowed",
                basis="بريد عمل منشور في صفحة التواصل",
            )
    return Seeded(product.id, company.id, contact.id, opp.id, draft.id, value)


async def approve(
    sm: async_sessionmaker[AsyncSession],
    ws: WorkspaceFixture,
    draft_id: uuid.UUID,
    *,
    reviewer: bool = False,
) -> tuple[Any, bool]:
    member = ws.reviewer if reviewer else ws.owner
    async with sm() as db, db.begin():
        from app.db.models_sales import Draft

        d = await db.get(Draft, draft_id)
        assert d is not None
        return await approvals.decide(
            db,
            make_settings(),
            workspace_id=ws.id,
            actor_id=f"user:{member.auth_user_id}",
            auth_user_id=member.auth_user_id,
            draft_id=draft_id,
            revision=d.revision,
            content_hash=d.content_hash,
            decision="approve",
        )


async def run_send(sm: async_sessionmaker[AsyncSession], deps: Deps) -> dict[str, Any] | None:
    async with sm() as db:
        lease = await queue.claim(db, "test-worker", kinds=["send_outbound"])
    if lease is None:
        return None
    return await send_outbound(deps, lease)


def inbound(
    uid: int,
    *,
    body: str,
    from_address: str = "buyer@clinic-waha.example",
    in_reply_to: str | None = None,
    auto: bool = False,
    message_id: str | None = None,
) -> InboundMail:
    return InboundMail(
        uid=str(uid),
        message_id=message_id or f"<in-{uid}-{uuid.uuid4().hex[:6]}@clinic-waha.example>",
        in_reply_to=in_reply_to,
        references=in_reply_to or "",
        from_address=from_address,
        from_name="",
        to_address="sales@example.com",
        subject="رد: تنظيم مواعيد العيادة",
        body_text=body,
        date=datetime.now(UTC),
        auto_submitted=auto,
    )
