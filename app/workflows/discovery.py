"""دورة الاكتشاف اليومية (LangGraph):

load_config → check_limits → plan_queries → discover → normalize_and_dedupe → preliminary_qualify
→ persist_candidates

دورة واحدة يوميًا لكل workspace (مفتاح فريد في الطابور)، بحدود: 3 استعلامات و20 مرشحًا افتراضيًا.
لا منتج أو مصدر صالح → skipped_configuration دون أي استدعاء نموذج. لا تُعاد كتابة الموصلات آليًا.
"""

from __future__ import annotations

import random
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.agents.gateway import CallCounter, ModelNotReady
from app.agents.prompts import PROMPT_VERSION, QUERY_SYSTEM, QueryPlan, context_block
from app.agents.providers import ProviderError
from app.api.errors import AppError
from app.connectors.search import SearchError
from app.connectors.web import HtmlConnector, RssConnector
from app.db.models import Product, ProductSegment, Segment, Source, SourceSegment
from app.db.models_ops import Run
from app.db.models_sales import Evidence, Opportunity
from app.services import budget, runs
from app.services import integrations as integ
from app.services.channels import ChannelNotReady, search_context
from app.services.companies import CompanyDraft, apply_draft
from app.services.normalize import normalize_name, website_identifier
from app.services.sources import source_config
from app.workflows.common import Deps, local_today, workspace_tz

DISCOVERABLE = ("web_search", "fake_search", "html", "rss")


class DiscoveryState(TypedDict, total=False):
    workspace_id: str
    run_id: str
    local_date: str
    status: str
    reason: str
    product_id: str
    segment_id: str
    source_id: str
    selection_reason: str
    queries: list[str]
    candidates: list[dict[str, Any]]
    stats: dict[str, int]


@dataclass
class Target:
    product: Product
    segment: Segment
    source: Source
    regions: list[str]
    reason: str


async def choose_target(deps: Deps, workspace_id: uuid.UUID, seed: str) -> Target | None:
    """اختيار منتج نشط + فئة متوافقة + مصدر نشط قابل للاكتشاف، بتوزيع مرجح بالأولوية مع منع التجويع."""
    async with deps.sm() as db:
        products = list(
            (
                await db.execute(
                    select(Product).where(
                        Product.workspace_id == workspace_id, Product.status == "active"
                    )
                )
            ).scalars()
        )
        options: list[tuple[Product, Segment, Source, list[str]]] = []
        for product in products:
            links = (
                (
                    await db.execute(
                        select(ProductSegment, Segment)
                        .join(
                            Segment,
                            (Segment.id == ProductSegment.segment_id)
                            & (Segment.workspace_id == ProductSegment.workspace_id),
                        )
                        .where(ProductSegment.product_id == product.id, Segment.status == "active")
                    )
                )
                .tuples()
                .all()
            )
            for ps, seg in links:
                sources = (
                    (
                        await db.execute(
                            select(Source).where(
                                Source.workspace_id == workspace_id,
                                Source.status == "active",
                                Source.connector_key.in_(DISCOVERABLE),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for src in sources:
                    src_segments = set(
                        (
                            await db.execute(
                                select(SourceSegment.segment_id).where(
                                    SourceSegment.source_id == src.id
                                )
                            )
                        ).scalars()
                    )
                    if src.connector_key == "html" and not src.allowed_paths:
                        continue  # موقع منشأة واحدة ليس دليلًا للاكتشاف
                    if src_segments and seg.id not in src_segments:
                        continue
                    options.append((product, seg, src, list(ps.regions or [])))
        if not options:
            return None
        last: dict[str, datetime] = {}
        rows = (
            await db.execute(
                select(Run.config_snapshot, Run.started_at)
                .where(
                    Run.workspace_id == workspace_id,
                    Run.kind == "discover",
                    Run.status == "completed",
                )
                .order_by(Run.started_at.desc())
                .limit(200)
            )
        ).all()
        for snap, at in rows:
            pid = (snap or {}).get("product_id")
            if pid and pid not in last:
                last[pid] = at
    now = datetime.now(UTC)
    weights = []
    for product, *_ in options:
        prev = last.get(str(product.id))
        days = 14 if prev is None else min(14, max(0, (now - prev).days))
        weights.append(product.priority * (1 + days))
    rng = random.Random(seed)  # noqa: S311 - حتمي لليوم نفسه: إعادة التشغيل لا تغير الاختيار (ليس غرضًا أمنيًا)
    idx = rng.choices(range(len(options)), weights=weights, k=1)[0]
    product, seg, src, regions = options[idx]
    prev = last.get(str(product.id))
    since = "لم يُختر من قبل" if prev is None else f"آخر اختيار قبل {(now - prev).days} يوم"
    reason = f"أولوية {product.priority}، {since}؛ {len(options)} تركيبة متاحة (وزن {weights[idx]} من {sum(weights)})"
    return Target(product, seg, src, regions or ["السعودية"], reason)


_AGGREGATORS = re.compile(
    r"(google\.|facebook\.|wikipedia\.|youtube\.|twitter\.|x\.com|tiktok\.|linkedin\.|amazon\.)",
    re.I,
)


def _prelim_score(text_: str, product: Product, segment: Segment) -> tuple[int, str | None]:
    words = {w for w in re.findall(r"\w+", normalize_name(text_)) if len(w) > 2}
    signal_words = {
        w
        for s in (product.fit_signals + segment.fit_rules)
        for w in re.findall(r"\w+", normalize_name(s))
        if len(w) > 2
    }
    for ex in product.exclusions + segment.exclusion_rules:
        ex_words = {w for w in re.findall(r"\w+", normalize_name(ex)) if len(w) > 2}
        if ex_words and ex_words <= words:
            return 0, f"استبعاد: {ex}"
    overlap = len(words & signal_words)
    return min(50, overlap * 10) + (10 if words else 0), None


def build_discovery_graph(deps: Deps) -> Any:
    async def load_config(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        async with deps.sm() as db:
            ops = await integ.get_config(
                db, deps.settings, ws, "operations", integ.OperationsConfig
            )
        if ops.pause_discovery or ops.pause_all_processing:
            return {"status": "canceled", "reason": "البحث موقوف من الإعدادات"}
        target = await choose_target(deps, ws, f"{ws}:{state['local_date']}")
        if target is None:
            return {
                "status": "skipped_configuration",
                "reason": "لا يوجد منتج نشط بفئة نشطة ومصدر نشط قابل للاكتشاف (بحث ويب، دليل HTML بمسارات، أو RSS)",
            }
        await runs.event(
            deps.sm,
            ws,
            uuid.UUID(state["run_id"]),
            "load_config",
            target.reason,
            data={"product": target.product.name, "source": target.source.name},
        )
        return {
            "product_id": str(target.product.id),
            "segment_id": str(target.segment.id),
            "source_id": str(target.source.id),
            "selection_reason": target.reason,
        }

    async def check_limits(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        async with deps.sm() as db:
            already = (
                await db.execute(
                    select(func.count())
                    .select_from(Run)
                    .where(
                        Run.workspace_id == ws,
                        Run.kind == "discover",
                        Run.status == "completed",
                        Run.config_snapshot["local_date"].astext == state["local_date"],
                    )
                )
            ).scalar_one()
        if already:
            return {"status": "canceled", "reason": "دورة اليوم نُفذت سابقًا"}
        return {}

    async def plan_queries(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        async with deps.sm() as db:
            product = await db.get(Product, uuid.UUID(state["product_id"]))
            segment = await db.get(Segment, uuid.UUID(state["segment_id"]))
            source = await db.get(Source, uuid.UUID(state["source_id"]))
            search_cfg = await integ.get_config(db, deps.settings, ws, "search", integ.SearchConfig)
            assert product and segment and source
            regions = (
                await db.execute(
                    select(ProductSegment.regions).where(
                        ProductSegment.product_id == product.id,
                        ProductSegment.segment_id == segment.id,
                    )
                )
            ).scalar_one()
        if source.connector_key not in ("web_search", "fake_search"):
            return {"queries": []}
        template = [f"{segment.name} {(regions or ['السعودية'])[0]}"]
        ctx = {
            "product": {
                "name": product.name,
                "problem": product.problem,
                "fit_signals": product.fit_signals,
            },
            "segment": {"name": segment.name, "description": segment.description},
            "regions": regions or ["السعودية"],
            "max_queries": search_cfg.max_queries,
        }
        try:
            plan, _ = await deps.gateway(ws).complete(
                role="extractor",
                system=QUERY_SYSTEM,
                user="اقترح استعلامات البحث.\n" + context_block(ctx),
                output_model=QueryPlan,
                schema_name="query_plan",
                category="discovery",
                counter=CallCounter(2),
                run_id=uuid.UUID(state["run_id"]),
                prompt_version=PROMPT_VERSION,
            )
            queries = [q.strip()[:200] for q in plan.queries if q.strip()][: search_cfg.max_queries]
        except (ModelNotReady, budget.BudgetBlocked, ProviderError, AppError) as exc:
            await runs.event(
                deps.sm,
                ws,
                uuid.UUID(state["run_id"]),
                "plan_queries",
                f"استُخدمت استعلامات قالبية: {exc}",
                event_type="warning",
            )
            queries = template
        return {"queries": queries or template}

    async def discover(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        run_id = uuid.UUID(state["run_id"])
        async with deps.sm() as db:
            source = await db.get(Source, uuid.UUID(state["source_id"]))
            search_cfg = await integ.get_config(db, deps.settings, ws, "search", integ.SearchConfig)
            ops = await integ.get_config(
                db, deps.settings, ws, "operations", integ.OperationsConfig
            )
        assert source
        limit = search_cfg.max_candidates
        cands: list[dict[str, Any]] = []
        config = source_config(source)
        if source.connector_key in ("web_search", "fake_search"):
            async with deps.sm() as db:
                try:
                    sctx = await search_context(db, deps.settings, ws)
                except ChannelNotReady as exc:
                    return {"status": "failed", "reason": str(exc)}
            per_query = max(1, -(-limit // max(1, len(state.get("queries", [])))))
            tz = await workspace_tz(deps.sm, ws, deps.settings.app_timezone)
            for q in state.get("queries", []):
                if len(cands) >= limit:
                    break
                reservation = None
                if sctx.client.paid:
                    async with deps.sm() as db, db.begin():
                        try:
                            reservation = await budget.reserve(
                                db,
                                ws,
                                ops,
                                amount=sctx.price_per_request or Decimal("0.01"),
                                category="discovery",
                                run_id=run_id,
                                tz_name=tz,
                            )
                        except budget.BudgetBlocked as exc:
                            await runs.event(
                                deps.sm, ws, run_id, "discover", str(exc), event_type="warning"
                            )
                            break
                try:
                    hits = await sctx.client.search(q, count=min(20, per_query))
                except SearchError as exc:
                    async with deps.sm() as db, db.begin():
                        await budget.release(db, reservation)
                    await runs.event(
                        deps.sm, ws, run_id, "discover", f"فشل البحث: {exc}", event_type="error"
                    )
                    continue
                if sctx.client.paid:
                    async with deps.sm() as db, db.begin():
                        await budget.settle(db, reservation, sctx.price_per_request)
                        budget.record_usage(
                            db,
                            workspace_id=ws,
                            run_id=run_id,
                            opportunity_id=None,
                            category="discovery",
                            kind="search",
                            provider=sctx.client.name,
                            model="",
                            role="search",
                            input_tokens=0,
                            output_tokens=0,
                            requests=1,
                            estimated_cost=sctx.price_per_request,
                            currency=search_cfg.currency,
                            pricing_version="owner-set",
                        )
                for h in hits:
                    cands.append(
                        {
                            "name": h.title,
                            "url": h.url,
                            "description": h.description,
                            "query": q,
                            "synthetic": h.synthetic,
                        }
                    )
        elif source.connector_key == "html":
            records, _ = await HtmlConnector(
                deps.settings, resolver=deps.resolver, transport=deps.transport
            ).list_directory(config, limit)
            cands = [
                {"name": r.fields.get("name", ""), "url": r.url, "description": "", "query": ""}
                for r in records
            ]
        elif source.connector_key == "rss":
            result = await RssConnector(
                deps.settings, resolver=deps.resolver, transport=deps.transport
            ).sample(config.__class__(**{**config.__dict__, "max_records": min(50, limit)}))
            cands = [
                {
                    "name": r.fields.get("name", ""),
                    "url": r.fields.get("link") or r.url or "",
                    "description": r.fields.get("description", ""),
                    "query": "",
                }
                for r in result.records
            ]
        seen: set[str] = set()
        unique = []
        for c in cands:
            if c["url"] and c["url"] not in seen and not _AGGREGATORS.search(c["url"]):
                seen.add(c["url"])
                unique.append(c)
        await runs.event(
            deps.sm,
            ws,
            run_id,
            "discover",
            f"{len(unique)} مرشح أولي من {source.name}",
            data={"queries": state.get("queries", []), "raw": len(cands)},
        )
        return {"candidates": unique[:limit]}

    async def normalize_and_dedupe(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        stats = {"created": 0, "linked": 0, "review": 0, "skipped": 0}
        out = []
        async with deps.sm() as db, db.begin():
            source = await db.get(Source, uuid.UUID(state["source_id"]))
            assert source
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"import:{ws}"}
            )
            for c in state.get("candidates", []):
                name = (c.get("name") or "").split(" - ")[0].split(" | ")[0].strip()[:300]
                if not name or not normalize_name(name):
                    stats["skipped"] += 1
                    continue
                draft = CompanyDraft(
                    display_name=name,
                    normalized_name=normalize_name(name),
                    source_record_ref=c["url"][:500],
                    source_url=c["url"][:2000],
                )
                try:
                    ident, domain = website_identifier(c["url"])
                except ValueError:
                    stats["skipped"] += 1
                    continue
                if ident:
                    draft.identifiers.append(ident)
                    draft.domain = domain
                result, company_id = await apply_draft(
                    db,
                    deps.settings,
                    workspace_id=ws,
                    source_id=source.id,
                    actor_id="service:discovery",
                    draft=draft,
                    import_batch_id=None,
                    is_demo=bool(c.get("synthetic")) or source.is_demo_data,
                )
                if company_id is None:
                    stats["skipped"] += 1
                    continue
                stats[
                    {"create": "created", "link": "linked", "review": "review"}.get(
                        result.action, "skipped"
                    )
                ] += 1
                if c.get("description") or c.get("name"):
                    claim = " — ".join(x for x in (c.get("name"), c.get("description")) if x)[:500]
                    ev = Evidence(
                        workspace_id=ws,
                        company_id=company_id,
                        source_id=source.id,
                        url=c["url"][:2000],
                        fact_type="listing",
                        claim=claim,
                        permitted_excerpt=(c.get("description") or "")[:500],
                        expires_at=datetime.now(UTC) + timedelta(days=source.retention_days),
                    )
                    db.add(ev)
                out.append({**c, "company_id": str(company_id), "action": result.action})
        return {"candidates": out, "stats": stats}

    async def preliminary_qualify(state: DiscoveryState) -> DiscoveryState:
        async with deps.sm() as db:
            product = await db.get(Product, uuid.UUID(state["product_id"]))
            segment = await db.get(Segment, uuid.UUID(state["segment_id"]))
        assert product and segment
        kept = []
        for c in state.get("candidates", []):
            score, excluded = _prelim_score(
                f"{c.get('name', '')} {c.get('description', '')}", product, segment
            )
            if excluded:
                continue
            kept.append({**c, "prelim": score})
        kept.sort(key=lambda c: c["prelim"], reverse=True)
        return {"candidates": kept}

    async def persist_candidates(state: DiscoveryState) -> DiscoveryState:
        ws = uuid.UUID(state["workspace_id"])
        stats = dict(state.get("stats", {}))
        new = 0
        async with deps.sm() as db, db.begin():
            for c in state.get("candidates", []):
                res = await db.execute(
                    insert(Opportunity)
                    .values(
                        workspace_id=ws,
                        company_id=uuid.UUID(c["company_id"]),
                        product_id=uuid.UUID(state["product_id"]),
                        segment_id=uuid.UUID(state["segment_id"]),
                        source_id=uuid.UUID(state["source_id"]),
                        status="discovered",
                        preliminary_score=c["prelim"],
                        selection_reason=state.get("selection_reason", ""),
                        discovered_run_id=uuid.UUID(state["run_id"]),
                        is_demo_data=bool(c.get("synthetic")),
                        next_action_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "company_id", "product_id"],
                        index_where=text(
                            "status NOT IN ('not_interested','won','lost','archived','disqualified')"
                        ),
                    )
                    .returning(Opportunity.id)
                )
                new += len(res.all())
            await runs.count_discovery(
                db, ws, local_today(await workspace_tz(deps.sm, ws, deps.settings.app_timezone))
            )
        stats["opportunities"] = new
        return {"stats": stats, "status": "completed"}

    def route(state: DiscoveryState) -> str:
        return (
            "stop"
            if state.get("status") in ("canceled", "skipped_configuration", "failed")
            else "next"
        )

    g = StateGraph(DiscoveryState)
    for name, fn in (
        ("load_config", load_config),
        ("check_limits", check_limits),
        ("plan_queries", plan_queries),
        ("discover", discover),
        ("normalize_and_dedupe", normalize_and_dedupe),
        ("preliminary_qualify", preliminary_qualify),
        ("persist_candidates", persist_candidates),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "load_config")
    g.add_conditional_edges("load_config", route, {"stop": END, "next": "check_limits"})
    g.add_conditional_edges("check_limits", route, {"stop": END, "next": "plan_queries"})
    g.add_edge("plan_queries", "discover")
    g.add_conditional_edges("discover", route, {"stop": END, "next": "normalize_and_dedupe"})
    g.add_edge("normalize_and_dedupe", "preliminary_qualify")
    g.add_edge("preliminary_qualify", "persist_candidates")
    g.add_edge("persist_candidates", END)
    return g.compile()


async def run_discovery(
    deps: Deps, workspace_id: uuid.UUID, *, job_id: uuid.UUID | None = None
) -> dict[str, Any]:
    tz = await workspace_tz(deps.sm, workspace_id, deps.settings.app_timezone)
    today = local_today(tz).isoformat()
    run_id = await runs.start_run(
        deps.sm, workspace_id, "discover", job_id=job_id, snapshot={"local_date": today}
    )
    graph = build_discovery_graph(deps)
    try:
        final = await graph.ainvoke(
            {"workspace_id": str(workspace_id), "run_id": str(run_id), "local_date": today}
        )
    except Exception as exc:
        await runs.finish_run(
            deps.sm,
            run_id,
            "failed",
            error_code=type(exc).__name__,
            summary={"error": str(exc)[:300]},
        )
        raise
    status = final.get("status", "completed")
    summary = {
        "reason": final.get("reason", ""),
        "queries": final.get("queries", []),
        "stats": final.get("stats", {}),
        "selection_reason": final.get("selection_reason", ""),
    }
    async with deps.sm() as db, db.begin():
        run = await db.get(Run, run_id)
        if run is not None:
            run.config_snapshot = {
                **run.config_snapshot,
                "product_id": final.get("product_id"),
                "source_id": final.get("source_id"),
                "segment_id": final.get("segment_id"),
            }
    await runs.finish_run(deps.sm, run_id, status, summary=summary)
    return {"run_id": str(run_id), "status": status, **summary}
