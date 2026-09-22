"""معالجة فرصة واحدة (LangGraph مع حافظ PostgreSQL دائم):

load_opportunity → eligibility_check → bounded_enrichment → evidence_validation → qualify
→ draft_message ⇄ policy_check → persist_draft → notify_review → wait_for_approval (interrupt)

الاعتماد لا يرسل داخل العقدة؛ خدمة الاعتماد تكتب أمر الإرسال، ثم يُستأنف الخيط نفسه للتوثيق فقط.
كل عقدة آمنة لإعادة التنفيذ بعد انقطاع (لا أثر خارجي قبل interrupt).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.agents.gateway import CallCounter, CallLimitReached, ModelNotReady, ModelOutputInvalid
from app.agents.prompts import (
    DRAFT_SYSTEM,
    PROMPT_VERSION,
    QUALIFY_SYSTEM,
    DraftOut,
    Qualification,
    context_block,
)
from app.agents.providers import ProviderError
from app.auth.security import data_hash
from app.connectors.base import SourceConfig
from app.connectors.web import HtmlConnector
from app.db.models import Company, Contact, Product, Segment, Suppression, Workspace
from app.db.models_sales import Draft, Evidence, ManualContact, Message, Opportunity
from app.jobs import queue
from app.services import approvals, budget, runs
from app.services import integrations as integ
from app.services.normalize import normalize_email, normalize_phone
from app.services.policy import check_draft
from app.workflows.checkpoint import checkpointer
from app.workflows.common import Deps

TERMINAL = ("canceled", "blocked_policy", "blocked_budget", "disqualified", "no_contact", "failed")
ENRICH_FRESH_DAYS = 30


class OppState(TypedDict, total=False):
    workspace_id: str
    opportunity_id: str
    run_id: str
    thread_id: str
    status: str
    reason: str
    evidence: list[dict[str, Any]]
    qualification: dict[str, Any]
    contact_id: str
    channel: str
    candidate: dict[str, Any]
    findings: list[dict[str, Any]]
    repaired: bool
    draft_id: str
    calls_used: int
    decision: Any


async def _ops(deps: Deps, ws: uuid.UUID) -> integ.OperationsConfig:
    async with deps.sm() as db:
        return await integ.get_config(db, deps.settings, ws, "operations", integ.OperationsConfig)


def _model_error_status(exc: Exception) -> str:
    return "blocked_budget" if isinstance(exc, budget.BudgetBlocked) else "blocked_policy"


def build_opportunity_graph(deps: Deps) -> StateGraph:
    async def load_opportunity(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        async with deps.sm() as db, db.begin():
            opp = (
                await db.execute(
                    select(Opportunity)
                    .where(Opportunity.id == opp_id, Opportunity.workspace_id == ws)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if opp is None:
                return {"status": "canceled", "reason": "الفرصة غير موجودة"}
            if opp.status not in ("discovered", "qualified"):
                return {"status": "canceled", "reason": f"حالة الفرصة {opp.status} لا تحتاج معالجة"}
            opp.graph_thread_id = state["thread_id"]
        return {"calls_used": state.get("calls_used", 0)}

    async def eligibility_check(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        ops = await _ops(deps, ws)
        if ops.pause_all_processing:
            return {"status": "canceled", "reason": "المعالجة موقوفة من الإعدادات"}
        async with deps.sm() as db, db.begin():
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            product = await db.get(Product, opp.product_id)
            company = await db.get(Company, opp.company_id)
            if product is None or product.status != "active":
                return {"status": "blocked_policy", "reason": "المنتج غير نشط"}
            assert company is not None
            hashes = [data_hash(deps.settings, "company", str(company.id))]
            if company.domain:
                hashes.append(data_hash(deps.settings, "domain", company.domain))
            suppressed = (
                await db.execute(
                    select(func.count())
                    .select_from(Suppression)
                    .where(Suppression.workspace_id == ws, Suppression.target_hash.in_(hashes))
                )
            ).scalar_one()
            if suppressed:
                opp.status, opp.disqualify_reason = "disqualified", "الجهة في سجل منع التواصل"
                return {"status": "disqualified", "reason": opp.disqualify_reason}
            since = datetime.now(UTC) - timedelta(days=ops.first_contact_cooldown_days)
            recent = (
                await db.execute(
                    select(func.max(Message.created_at)).where(
                        Message.workspace_id == ws,
                        Message.company_id == company.id,
                        Message.direction == "outbound",
                    )
                )
            ).scalar_one()
            manual = (
                await db.execute(
                    select(func.max(ManualContact.contacted_at))
                    .join(Opportunity, Opportunity.id == ManualContact.opportunity_id)
                    .where(Opportunity.company_id == company.id)
                )
            ).scalar_one()
            last = max([d for d in (recent, manual) if d is not None], default=None)
            if last is not None and last > since:
                opp.next_action_at = last + timedelta(days=ops.first_contact_cooldown_days)
                return {
                    "status": "blocked_policy",
                    "reason": f"تواصلنا مع الجهة خلال {ops.first_contact_cooldown_days} يومًا (أي منتج)؛ يحتاج مراجعة بشرية",
                }
            pending = (
                await db.execute(
                    select(func.count())
                    .select_from(Draft)
                    .join(Opportunity, Opportunity.id == Draft.opportunity_id)
                    .where(
                        Opportunity.company_id == company.id,
                        Draft.status.in_(("pending_review", "approved", "deferred")),
                    )
                )
            ).scalar_one()
            if pending:
                return {"status": "blocked_policy", "reason": "توجد مسودة معلقة لنفس الجهة"}
        return {}

    async def bounded_enrichment(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        run_id = uuid.UUID(state["run_id"])
        ops = await _ops(deps, ws)
        async with deps.sm() as db:
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            company = await db.get(Company, opp.company_id)
            assert company is not None
            fresh = (
                await db.execute(
                    select(func.count())
                    .select_from(Evidence)
                    .where(
                        Evidence.company_id == company.id,
                        Evidence.fact_type.in_(("website", "contact_page")),
                        Evidence.observed_at
                        > datetime.now(UTC) - timedelta(days=ENRICH_FRESH_DAYS),
                    )
                )
            ).scalar_one()
            ws_row = await db.get(Workspace, ws)
        if (
            ops.max_enrichment_pages == 0
            or not company.domain
            or fresh
            or not deps.settings.source_fetch_enabled
        ):
            await runs.event(
                deps.sm,
                ws,
                run_id,
                "bounded_enrichment",
                "تخطي الإثراء (لا نطاق، أو أدلة حديثة، أو الجلب معطل)",
            )
            return {}
        domain = company.domain.removeprefix("www.")
        config = SourceConfig(
            source_id=opp.source_id or uuid.UUID(int=0),
            workspace_id=ws,
            kind="html",
            connector_key="html",
            url=f"https://{domain}/",
            allowed_hosts=(domain, f"www.{domain}"),
            allowed_paths=(),
            max_pages=max(1, ops.max_enrichment_pages),
            max_records=1,
            max_requests=ops.max_enrichment_pages + 2,
            timeout_seconds=45,
            credential_ref=None,
            store_raw=False,
            retention_days=90,
            config_version=0,
        )
        result = await HtmlConnector(
            deps.settings, resolver=deps.resolver, transport=deps.transport
        ).sample(config)
        if result.status != "succeeded" or not result.records:
            await runs.event(
                deps.sm,
                ws,
                run_id,
                "bounded_enrichment",
                f"تعذر الإثراء: {result.message}",
                event_type="warning",
            )
            return {}
        rec = result.records[0]
        fields = rec.fields
        async with deps.sm() as db, db.begin():
            expires = datetime.now(UTC) + timedelta(days=90)
            claim = " — ".join(x for x in (fields.get("name"), fields.get("description")) if x)
            if claim:
                db.add(
                    Evidence(
                        workspace_id=ws,
                        company_id=company.id,
                        source_id=opp.source_id,
                        url=rec.url,
                        fact_type="website",
                        claim=claim[:500],
                        permitted_excerpt=(fields.get("description") or "")[:500],
                        expires_at=expires,
                    )
                )
            if fields.get("category"):
                db.add(
                    Evidence(
                        workspace_id=ws,
                        company_id=company.id,
                        source_id=opp.source_id,
                        url=rec.url,
                        fact_type="website",
                        claim=f"تصنيف النشاط المعلن: {fields['category']}"[:500],
                        expires_at=expires,
                    )
                )
            prov = {"source": "website", "url": rec.url, "note": "قناة عمل منشورة في موقع الجهة"}
            if fields.get("email"):
                try:
                    email = normalize_email(fields["email"])
                    await db.execute(
                        insert(Contact)
                        .values(
                            workspace_id=ws,
                            company_id=company.id,
                            channel="email",
                            value=email,
                            normalized=True,
                            value_hash=data_hash(deps.settings, "email", email.lower()),
                            provenance=prov,
                        )
                        .on_conflict_do_nothing()
                    )
                except ValueError:
                    pass
            if fields.get("phone"):
                try:
                    value, ok = normalize_phone(
                        fields["phone"], ws_row.default_phone_region if ws_row else None
                    )
                    await db.execute(
                        insert(Contact)
                        .values(
                            workspace_id=ws,
                            company_id=company.id,
                            channel="phone",
                            value=value.removeprefix("raw:"),
                            normalized=ok,
                            value_hash=data_hash(deps.settings, "phone", value),
                            provenance=prov,
                        )
                        .on_conflict_do_nothing()
                    )
                except ValueError:
                    pass
        await runs.event(
            deps.sm,
            ws,
            run_id,
            "bounded_enrichment",
            f"{result.pages_fetched} صفحة، {result.request_count} طلب",
            data={"fields": sorted(fields)},
        )
        return {}

    async def evidence_validation(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        async with deps.sm() as db, db.begin():
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            rows = list(
                (
                    await db.execute(
                        select(Evidence)
                        .where(
                            Evidence.company_id == opp.company_id,
                            Evidence.workspace_id == ws,
                            Evidence.expires_at > datetime.now(UTC),
                        )
                        .order_by(Evidence.observed_at.desc())
                        .limit(12)
                    )
                ).scalars()
            )
            if not rows:
                opp.status, opp.disqualify_reason = (
                    "disqualified",
                    "لا توجد أدلة موثقة صالحة؛ لا تُكتب رسالة تزعم الحاجة",
                )
                return {"status": "disqualified", "reason": opp.disqualify_reason}
        return {
            "evidence": [
                {"id": str(e.id), "fact_type": e.fact_type, "claim": e.claim, "url": e.url}
                for e in rows
            ]
        }

    async def qualify(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        run_id = uuid.UUID(state["run_id"])
        ops = await _ops(deps, ws)
        async with deps.sm() as db:
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            product = await db.get(Product, opp.product_id)
            company = await db.get(Company, opp.company_id)
            segment = await db.get(Segment, opp.segment_id) if opp.segment_id else None
            contacts = list(
                (
                    await db.execute(select(Contact).where(Contact.company_id == opp.company_id))
                ).scalars()
            )
        assert product and company
        email_contacts = [c for c in contacts if c.channel == "email"]
        phone_contacts = [c for c in contacts if c.channel == "phone"]
        ctx = {
            "product": {
                "name": product.name,
                "problem": product.problem,
                "capabilities": product.capabilities,
                "unavailable": product.unavailable_capabilities,
                "fit_signals": product.fit_signals,
                "exclusions": product.exclusions,
            },
            "segment": {
                "name": segment.name,
                "fit_rules": segment.fit_rules,
                "exclusion_rules": segment.exclusion_rules,
            }
            if segment
            else {},
            "company": {
                "name": company.display_name,
                "sector": company.sector,
                "region": company.region,
                "domain": company.domain,
            },
            "evidence": state.get("evidence", []),
            "contact": {"channels": sorted({c.channel for c in contacts})},
        }
        counter = CallCounter(ops.max_model_calls_per_opportunity, used=state.get("calls_used", 0))
        try:
            q, _ = await deps.gateway(ws).complete(
                role="writer",
                system=QUALIFY_SYSTEM,
                user="قيّم هذه الفرصة.\n" + context_block(ctx),
                output_model=Qualification,
                schema_name="qualification",
                category="new_opportunity",
                counter=counter,
                run_id=run_id,
                opportunity_id=opp_id,
                prompt_version=PROMPT_VERSION,
            )
        except (
            ModelNotReady,
            budget.BudgetBlocked,
            ProviderError,
            ModelOutputInvalid,
            CallLimitReached,
        ) as exc:
            return {
                "status": _model_error_status(exc),
                "reason": str(exc),
                "calls_used": counter.used,
            }
        valid_ids = {e["id"] for e in state.get("evidence", [])}
        facts = [f.model_dump() for f in q.facts if f.evidence_id in valid_ids]
        parts = q.score.model_dump()
        total = sum(parts.values())
        passes = (
            q.fit == "qualified"
            and q.recommended_action == "prepare_draft"
            and total >= ops.score_threshold
            and len(facts) >= 1
            and parts["product_fit"] >= 20
        )
        missing = list(q.unknowns)
        async with deps.sm() as db, db.begin():
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            opp.score, opp.score_breakdown, opp.facts = total, parts, facts
            opp.hypotheses, opp.evidence_ids = q.hypotheses, [f["evidence_id"] for f in facts]
            if not passes:
                dropped = len(q.facts) - len(facts)
                opp.status = "disqualified"
                opp.disqualify_reason = (
                    f"لم تجتز التأهيل: الدرجة {total} (الحد {ops.score_threshold})، أدلة موثقة {len(facts)}"
                    + (f"، أُسقطت {dropped} حقيقة بلا دليل صالح" if dropped else "")
                    + f". {q.reason[:200]}"
                )
                opp.missing_info = missing
                return {
                    "status": "disqualified",
                    "reason": opp.disqualify_reason,
                    "calls_used": counter.used,
                }
            opp.status = "qualified"
            contact: Contact | None = next(iter(email_contacts or phone_contacts), None)
            if contact is None:
                opp.missing_info = [*missing, "لا توجد قناة تواصل (بريد أو هاتف) للجهة"]
                return {
                    "status": "no_contact",
                    "reason": "مؤهلة لكن بلا قناة تواصل؛ أضف جهة اتصال",
                    "calls_used": counter.used,
                }
            opp.missing_info = missing
        return {
            "qualification": {**q.model_dump(), "facts": facts, "total": total},
            "calls_used": counter.used,
            "contact_id": str(contact.id),
            "channel": "email" if contact.channel == "email" else "whatsapp",
        }

    async def draft_message(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        ops = await _ops(deps, ws)
        async with deps.sm() as db:
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            product = await db.get(Product, opp.product_id)
            company = await db.get(Company, opp.company_id)
            contact = await db.get(Contact, uuid.UUID(state["contact_id"]))
        assert product and company
        evidence_by_id = {e["id"]: e for e in state.get("evidence", [])}
        facts = [
            {
                "claim": evidence_by_id[f["evidence_id"]]["claim"][:300],
                "evidence_id": f["evidence_id"],
            }
            for f in state.get("qualification", {}).get("facts", [])
            if f["evidence_id"] in evidence_by_id
        ]
        ctx: dict[str, Any] = {
            "product": {
                "name": product.name,
                "summary": product.summary,
                "problem": product.problem,
                "capabilities": product.capabilities,
                "unavailable": product.unavailable_capabilities,
                "demo_url": product.demo_url or "",
                "price": product.price_text
                if product.price_status == "approved"
                else "غير معتمد — لا تذكر سعرًا",
            },
            "company": {"name": company.display_name, "sector": company.sector},
            "facts": facts,
            "hypotheses": state.get("qualification", {}).get("hypotheses", []),
            "recipient_name": contact.professional_name if contact else "",
            "channel": state.get("channel", "email"),
        }
        user = "اكتب الرسالة الأولى.\n" + context_block(ctx)
        if state.get("findings"):
            user += "\nالمسودة السابقة خالفت السياسة؛ أصلح هذه النقاط تحديدًا:\n" + "\n".join(
                f"- {f['message']}" for f in state["findings"] if f.get("blocking")
            )
        counter = CallCounter(ops.max_model_calls_per_opportunity, used=state.get("calls_used", 0))
        try:
            d, usage = await deps.gateway(ws).complete(
                role="writer",
                system=DRAFT_SYSTEM,
                user=user,
                output_model=DraftOut,
                schema_name="first_contact_draft",
                category="new_opportunity",
                counter=counter,
                run_id=uuid.UUID(state["run_id"]),
                opportunity_id=opp_id,
                prompt_version=PROMPT_VERSION,
            )
        except (
            ModelNotReady,
            budget.BudgetBlocked,
            ProviderError,
            ModelOutputInvalid,
            CallLimitReached,
        ) as exc:
            return {
                "status": _model_error_status(exc),
                "reason": str(exc),
                "calls_used": counter.used,
            }
        return {
            "candidate": {
                **d.model_dump(),
                "model": f"{usage.provider}/{usage.model}",
                "cost": str(usage.cost),
            },
            "calls_used": counter.used,
        }

    async def policy_check(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        async with deps.sm() as db:
            opp = await db.get(Opportunity, opp_id)
            assert opp is not None
            product = await db.get(Product, opp.product_id)
            company = await db.get(Company, opp.company_id)
            mail = await integ.get_config(db, deps.settings, ws, "mail", integ.MailConfig)
        assert product and company
        cand = state["candidate"]
        support = [e["claim"] for e in state.get("evidence", [])] + [company.display_name]
        findings = [
            f.as_dict()
            for f in check_draft(
                subject=cand["subject"],
                body=cand["body"],
                product=product,
                capabilities_used=cand["capabilities_used"],
                support_texts=support,
                allowed_urls=[product.product_url or "", product.demo_url or "", mail.booking_link],
            )
        ]
        valid = {e["id"] for e in state.get("evidence", [])}
        for ev in cand.get("evidence_ids_used", []):
            if ev not in valid:
                findings.append(
                    {
                        "code": "unknown_evidence",
                        "message": f"دليل غير موجود: {ev}",
                        "blocking": True,
                    }
                )
        return {"findings": findings}

    def after_policy(state: OppState) -> str:
        if state.get("status") in TERMINAL:
            return "stop"
        blocking = any(f.get("blocking") for f in state.get("findings", []))
        return "repair" if blocking and not state.get("repaired") else "persist"

    async def mark_repair(state: OppState) -> OppState:
        return {"repaired": True}

    async def persist_draft(state: OppState) -> OppState:
        ws, opp_id = uuid.UUID(state["workspace_id"]), uuid.UUID(state["opportunity_id"])
        async with deps.sm() as db, db.begin():
            opp = (
                await db.execute(
                    select(Opportunity).where(Opportunity.id == opp_id).with_for_update()
                )
            ).scalar_one()
            existing = (
                (
                    await db.execute(
                        select(Draft).where(
                            Draft.opportunity_id == opp_id,
                            Draft.kind == "first_contact",
                            Draft.status.in_(("pending_review", "approved", "deferred")),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:  # إعادة تنفيذ العقدة بعد انقطاع لا تكرر المسودة
                return {"draft_id": str(existing.id)}
            product = await db.get(Product, opp.product_id)
            contact = await db.get(Contact, uuid.UUID(state["contact_id"]))
            mail = await integ.get_config(db, deps.settings, ws, "mail", integ.MailConfig)
            assert product
            cand = state["candidate"]
            tail = [x for x in (mail.signature.strip(), mail.opt_out_line.strip()) if x]
            body = cand["body"].strip() + ("\n\n" + "\n\n".join(tail) if tail else "")
            draft = await approvals.create_draft(
                db,
                workspace_id=ws,
                opportunity=opp,
                kind="first_contact",
                channel=state.get("channel", "email"),
                contact=contact,
                subject=cand["subject"].strip(),
                body=body,
                product_version=product.version,
                findings=state.get("findings", []),
                created_by="service:writer",
                model_meta={
                    "model": cand.get("model"),
                    "cost": cand.get("cost"),
                    "prompt_version": PROMPT_VERSION,
                    "capabilities_used": cand.get("capabilities_used", []),
                    "evidence_ids_used": cand.get("evidence_ids_used", []),
                    "repaired": bool(state.get("repaired")),
                },
            )
            opp.status = "draft_ready"
        return {"draft_id": str(draft.id)}

    async def notify_review(state: OppState) -> OppState:
        ws = uuid.UUID(state["workspace_id"])
        async with deps.sm() as db, db.begin():
            await queue.enqueue(
                db,
                kind="notify_draft",
                workspace_id=ws,
                payload={"draft_id": state["draft_id"]},
                idempotency_key=f"notify:{state['draft_id']}:1",
                max_attempts=3,
            )
        return {}

    async def wait_for_approval(state: OppState) -> OppState:
        decision = interrupt(
            {"draft_id": state.get("draft_id"), "opportunity_id": state["opportunity_id"]}
        )
        return {"decision": decision, "status": "completed"}

    def stop_or(next_node: str) -> Any:
        def route(state: OppState) -> str:
            return END if state.get("status") in TERMINAL else next_node

        return route

    g = StateGraph(OppState)
    for name, fn in (
        ("load_opportunity", load_opportunity),
        ("eligibility_check", eligibility_check),
        ("bounded_enrichment", bounded_enrichment),
        ("evidence_validation", evidence_validation),
        ("qualify", qualify),
        ("draft_message", draft_message),
        ("policy_check", policy_check),
        ("mark_repair", mark_repair),
        ("persist_draft", persist_draft),
        ("notify_review", notify_review),
        ("wait_for_approval", wait_for_approval),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "load_opportunity")
    g.add_conditional_edges("load_opportunity", stop_or("eligibility_check"))
    g.add_conditional_edges("eligibility_check", stop_or("bounded_enrichment"))
    g.add_edge("bounded_enrichment", "evidence_validation")
    g.add_conditional_edges("evidence_validation", stop_or("qualify"))
    g.add_conditional_edges("qualify", stop_or("draft_message"))
    g.add_conditional_edges("draft_message", stop_or("policy_check"))
    g.add_conditional_edges(
        "policy_check",
        after_policy,
        {"stop": END, "repair": "mark_repair", "persist": "persist_draft"},
    )
    g.add_edge("mark_repair", "draft_message")
    g.add_edge("persist_draft", "notify_review")
    g.add_edge("notify_review", "wait_for_approval")
    g.add_edge("wait_for_approval", END)
    return g


def thread_for(opportunity_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"opp:{opportunity_id}:{run_id}"


async def run_opportunity(
    deps: Deps,
    workspace_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    run_id = await runs.start_run(
        deps.sm, workspace_id, "process_opportunity", job_id=job_id, opportunity_id=opportunity_id
    )
    thread_id = thread_for(opportunity_id, run_id)
    config = {"configurable": {"thread_id": thread_id}}
    initial: OppState = {
        "workspace_id": str(workspace_id),
        "opportunity_id": str(opportunity_id),
        "run_id": str(run_id),
        "thread_id": thread_id,
        "calls_used": 0,
    }
    try:
        async with checkpointer(deps.settings) as saver:
            graph = build_opportunity_graph(deps).compile(checkpointer=saver)
            final = await graph.ainvoke(initial, config)  # type: ignore[call-overload]
            snapshot = await graph.aget_state(config)  # type: ignore[arg-type]
    except Exception as exc:
        await runs.finish_run(
            deps.sm,
            run_id,
            "failed",
            error_code=type(exc).__name__,
            summary={"error": str(exc)[:300]},
            thread_id=thread_id,
        )
        raise
    waiting = bool(snapshot.next)
    status = "waiting_approval" if waiting else final.get("status", "completed")
    if status in ("disqualified", "no_contact"):
        status = "completed"
    summary = {
        "reason": final.get("reason", ""),
        "draft_id": final.get("draft_id"),
        "calls": final.get("calls_used", 0),
        "outcome": final.get("status", "waiting" if waiting else "completed"),
    }
    await runs.finish_run(deps.sm, run_id, status, summary=summary, thread_id=thread_id)
    return {"run_id": str(run_id), "status": status, "thread_id": thread_id, **summary}


async def resume_opportunity(deps: Deps, thread_id: str, decision: Any) -> dict[str, Any]:
    """استئناف الخيط نفسه بعد القرار (قد يكون العامل أعيد تشغيله بينهما)."""
    config = {"configurable": {"thread_id": thread_id}}
    async with checkpointer(deps.settings) as saver:
        graph = build_opportunity_graph(deps).compile(checkpointer=saver)
        snapshot = await graph.aget_state(config)  # type: ignore[arg-type]
        if not snapshot.next:
            return {"status": "not_waiting"}
        final = await graph.ainvoke(Command(resume=decision), config)  # type: ignore[call-overload]
    match = re.match(r"^opp:[0-9a-f-]+:([0-9a-f-]{36})$", thread_id)
    if match:
        await runs.finish_run(
            deps.sm,
            uuid.UUID(match.group(1)),
            "completed",
            summary={"decision": decision, "draft_id": final.get("draft_id")},
        )
    return {"status": "completed", "decision": decision}


async def next_candidate(
    deps: Deps, workspace_id: uuid.UUID, exclude: list[uuid.UUID]
) -> uuid.UUID | None:
    async with deps.sm() as db:
        q = (
            select(Opportunity.id)
            .where(
                Opportunity.workspace_id == workspace_id,
                Opportunity.status == "discovered",
                or_(
                    Opportunity.next_action_at.is_(None),
                    Opportunity.next_action_at <= datetime.now(UTC),
                ),
            )
            .order_by(Opportunity.preliminary_score.desc(), Opportunity.created_at)
            .limit(1)
        )
        if exclude:
            q = q.where(Opportunity.id.notin_(exclude))
        return (await db.execute(q)).scalar_one_or_none()
