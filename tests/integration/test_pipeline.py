"""M2: اكتشاف محدود → تأهيل بالأدلة → مسودة → انتظار الاعتماد (F-006، F-007، T01، T08، T09)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Company,
    Contact,
    Job,
    Product,
    ProductSegment,
    Segment,
    Source,
    SourceSegment,
)
from app.db.models_ops import DailyQuota, Run
from app.db.models_sales import Draft, Evidence, Opportunity
from app.workflows.common import Deps
from app.workflows.discovery import run_discovery
from app.workflows.opportunity import build_opportunity_graph, resume_opportunity, run_opportunity
from app.workflows.processing import LockBusy, run_window, sales_lock
from tests.conftest import WorkspaceFixture, make_settings
from tests.fakes import FakeNet

CLINIC_SITE = """<html><head><title>مجمع عيادات الواحة</title>
<meta name="description" content="مجمع عيادات أسنان وجلدية في الرياض. الحجز عبر الهاتف خلال ساعات الدوام."></head>
<body><h1>مجمع عيادات الواحة</h1><a href="mailto:info@clinic-waha.example">راسلنا</a></body></html>"""


def fake_net() -> FakeNet:
    net = FakeNet()
    for host in ("clinic-waha.example", "bun-madina.example", "nakheel-stays.example"):
        net.add_site(host)
        import httpx

        net.route(host, "/robots.txt", httpx.Response(404))
    net.html("clinic-waha.example", "/", CLINIC_SITE)
    net.html(
        "bun-madina.example",
        "/",
        "<html><head><title>بن المدينة</title></head><body><p>قهوة مختصة</p></body></html>",
    )
    return net


async def setup_catalog(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture, *, source_kind: str = "web_search"
) -> dict[str, Any]:
    async with sm() as db, db.begin():
        seg = Segment(
            workspace_id=ws.id, name="عيادات", fit_rules=["تعمل بالمواعيد"], exclusion_rules=[]
        )
        db.add(seg)
        await db.flush()
        product = Product(
            workspace_id=ws.id,
            name="منظم المواعيد",
            status="active",
            priority=4,
            problem="ضياع الحجوزات بسبب الاعتماد على الهاتف",
            summary="صفحة حجز وتذكير",
            capabilities=["صفحة حجز عامة", "تذكير بالموعد عبر البريد"],
            unavailable_capabilities=["تكامل السجلات الطبية"],
            fit_signals=["الحجز عبر الهاتف"],
            exclusions=[],
            price_status="needs_review",
        )
        db.add(product)
        await db.flush()
        db.add(
            ProductSegment(
                workspace_id=ws.id, product_id=product.id, segment_id=seg.id, regions=["الرياض"]
            )
        )
        src = Source(
            workspace_id=ws.id,
            name="بحث",
            kind=source_kind,
            connector_key="web_search",
            access_mode="api_key",
            status="active",
        )
        db.add(src)
        await db.flush()
        db.add(SourceSegment(workspace_id=ws.id, source_id=src.id, segment_id=seg.id))
    return {"product": product.id, "segment": seg.id, "source": src.id}


def deps(sm: async_sessionmaker[AsyncSession], net: FakeNet | None = None) -> Deps:
    return Deps(
        settings=make_settings(),
        sm=sm,
        resolver=(net or fake_net()).resolve,
        transport=(net or fake_net()).transport,
    )


async def test_discovery_is_bounded_idempotent_and_skips_without_config(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    d = deps(sm)
    skipped = await run_discovery(d, ws.id)
    assert skipped["status"] == "skipped_configuration"
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Run))).scalar_one() == 1
    await setup_catalog(sm, ws)
    first = await run_discovery(d, ws.id)
    assert first["status"] == "completed", first
    assert first["stats"]["opportunities"] == 3 and first["stats"]["created"] == 3
    assert 1 <= len(first["queries"]) <= 3
    second = await run_discovery(d, ws.id)
    assert second["status"] == "canceled"  # لا تكرار لدورة اليوم
    async with sm() as db:
        opps = (await db.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        evidence = (await db.execute(select(func.count()).select_from(Evidence))).scalar_one()
        quota = (await db.execute(select(DailyQuota))).scalar_one()
    assert opps == 3 and evidence == 3 and quota.discovery_count == 1


async def test_window_qualifies_with_evidence_and_waits_for_approval(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    net = fake_net()
    d = deps(sm, net)
    await setup_catalog(sm, ws)
    await run_discovery(d, ws.id)
    result = await run_window(d, ws.id)
    assert result["status"] == "draft_ready", result
    async with sm() as db:
        draft = (await db.execute(select(Draft))).scalar_one()
        opp = await db.get(Opportunity, draft.opportunity_id)
        company = await db.get(Company, opp.company_id)  # type: ignore[union-attr]
        contact = await db.get(Contact, draft.recipient_contact_id)
        run = (
            (await db.execute(select(Run).where(Run.kind == "process_opportunity")))
            .scalars()
            .first()
        )
        jobs = [j.kind for j in (await db.execute(select(Job))).scalars()]
        quota = (await db.execute(select(DailyQuota))).scalar_one()
    assert company is not None and "الواحة" in company.display_name
    assert (
        opp is not None
        and opp.status == "draft_ready"
        and opp.score is not None
        and opp.score >= 70
    )
    assert all(f["evidence_id"] in {str(e) for e in opp.evidence_ids} for f in opp.facts)
    assert contact is not None and contact.value == "info@clinic-waha.example"  # من الإثراء المحدود
    assert draft.status == "pending_review" and draft.channel == "email"
    assert "إيقاف" in draft.body  # سطر إيقاف التواصل يضاف آليًا
    assert not any(f["blocking"] for f in draft.policy_findings)
    assert run is not None and run.status == "waiting_approval"
    assert quota.qualified_slots_used == 1 and quota.qualified_slots_reserved == 1
    assert "notify_draft" in jobs

    # T09: استئناف بعد «إعادة تشغيل» (حافظ جديد) على الخيط نفسه من الحفظ الدائم
    assert opp.graph_thread_id
    resumed = await resume_opportunity(d, opp.graph_thread_id, "approve")
    assert resumed["status"] == "completed"
    async with sm() as db:
        run = await db.get(Run, run.id)
    assert run is not None and run.status == "completed"


async def test_no_evidence_means_no_draft(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T08: لا دليل حاجة → لا رسالة تزعم وجود المشكلة."""
    ids = await setup_catalog(sm, ws)
    async with sm() as db, db.begin():
        company = Company(
            workspace_id=ws.id, display_name="جهة بلا أدلة", normalized_name="جهه بلا ادله"
        )
        db.add(company)
        await db.flush()
        opp = Opportunity(
            workspace_id=ws.id,
            company_id=company.id,
            product_id=ids["product"],
            segment_id=ids["segment"],
        )
        db.add(opp)
    result = await run_opportunity(deps(sm), ws.id, opp.id)
    assert result["status"] == "completed" and result["outcome"] == "disqualified"
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Draft))).scalar_one() == 0
        opp2 = await db.get(Opportunity, opp.id)
    assert (
        opp2 is not None
        and opp2.status == "disqualified"
        and "أدلة" in (opp2.disqualify_reason or "")
    )


async def test_quota_counts_across_all_products(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    """T01/T17: الحصة إجمالية للـworkspace وليست لكل منتج، والحجز ذري."""
    from app.services import integrations as integ
    from app.services import runs as run_service

    async with sm() as db, db.begin():
        await integ.save_config(
            db,
            ws.id,
            "operations",
            integ.OperationsConfig(max_qualified_per_day=2),
            version=0,
            actor_id="t",
        )
    import asyncio
    from datetime import date

    async def grab() -> bool:
        async with sm() as db, db.begin():
            return await run_service.reserve_slot(db, ws.id, date(2026, 9, 22), 2)

    assert sum(await asyncio.gather(*(grab() for _ in range(6)))) == 2


async def test_sales_lock_allows_one_processor(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    d = deps(sm)
    async with sales_lock(d, ws.id):
        with pytest.raises(LockBusy):
            async with sales_lock(d, ws.id):
                pass
    async with sales_lock(d, ws.id):
        pass


async def test_graph_compiles_with_expected_nodes() -> None:
    g = build_opportunity_graph(Deps(settings=make_settings(), sm=None))  # type: ignore[arg-type]
    assert {
        "eligibility_check",
        "bounded_enrichment",
        "evidence_validation",
        "qualify",
        "draft_message",
        "policy_check",
        "persist_draft",
        "notify_review",
        "wait_for_approval",
    } <= set(g.nodes)
