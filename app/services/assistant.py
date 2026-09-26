"""مساعد تيليجرام للأعضاء: أوامر سريعة بلا نموذج، وأسئلة حرة يجيب عنها النموذج من لقطة قراءة فقط.

- لا ينفذ أي إجراء (لا اعتماد ولا إرسال ولا تعديل)؛ الاعتماد يبقى لأزرار البطاقات واللوحة.
- اللقطة محصورة بالـworkspace، ولا تتضمن قيم جهات الاتصال (بريد أو هاتف): لا نرسل بيانات شخصية
  لمزود النموذج بلا حاجة.
- الاستدعاء يمر بالبوابة المعتادة (سعر، موافقة المعالجة، ميزانية بفئة «assistant»، سجل استهلاك).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway import CallCounter, CallLimitReached, ModelNotReady, ModelOutputInvalid
from app.agents.prompts import ASSISTANT_SYSTEM, PROMPT_VERSION, AssistantAnswer, context_block
from app.agents.providers import ProviderError
from app.api.errors import AppError
from app.config import Settings
from app.db.models import Company, Product, Workspace
from app.db.models_ops import DailyQuota, Run
from app.db.models_sales import Draft, Message, Opportunity, OutboundCommand
from app.services import budget
from app.services import integrations as integ
from app.services.normalize import normalize_name
from app.workflows.common import Deps

CENTS = Decimal("0.01")

HELP_TEXT = (
    "أنا مساعد وكيل المبيعات. أقرأ بيانات شركتك ولا أنفذ أي إجراء.\n\n"
    "/today — ملخص اليوم\n"
    "/pending — المسودات بانتظار اعتمادك\n"
    "/ask سؤالك — سؤال حر (في المحادثة الخاصة اكتب السؤال مباشرة)\n\n"
    "أمثلة: ما حالة مجمع عيادات الواحة؟ · كم فرصة تأهلت هذا الأسبوع؟ · لماذا رُفضت آخر فرصة؟\n"
    "الاعتماد والإرسال من أزرار البطاقات أو اللوحة فقط."
)

STATUS_AR = {
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


def _fmt(dt: datetime | None, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "—"


async def _ws(db: AsyncSession, workspace_id: uuid.UUID) -> tuple[Workspace, ZoneInfo]:
    ws = await db.get(Workspace, workspace_id)
    assert ws is not None
    return ws, ZoneInfo(ws.timezone)


async def today_facts(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> dict[str, Any]:
    ws, tz = await _ws(db, workspace_id)
    ops = await integ.get_config(db, settings, workspace_id, "operations", integ.OperationsConfig)
    local_date = datetime.now(UTC).astimezone(tz).date()
    quota = await db.get(DailyQuota, (workspace_id, local_date))

    async def n(model: Any, *where: Any) -> int:
        q = (
            select(func.count())
            .select_from(model)
            .where(model.workspace_id == workspace_id, *where)
        )
        return int((await db.execute(q)).scalar_one())

    last = (
        await db.execute(
            select(Run)
            .where(Run.workspace_id == workspace_id, Run.kind == "discover")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    usage = await budget.usage_summary(db, workspace_id, ws.timezone)
    return {
        "date": str(local_date),
        "mode": ops.operating_mode,
        "pending_approvals": await n(Draft, Draft.status == "pending_review"),
        "qualified_today": quota.qualified_slots_used if quota else 0,
        "daily_quota": ops.max_qualified_per_day,
        "discovered_today": quota.discovery_count if quota else 0,
        "unknown_delivery": await n(OutboundCommand, OutboundCommand.status == "unknown_delivery"),
        "needs_linking": await n(Message, Message.link_status == "needs_linking"),
        "spent_today": str(usage["day_spent"].quantize(CENTS)),
        "spent_month": str(usage["month_spent"].quantize(CENTS)),
        "daily_budget": str(ops.daily_budget) if ops.daily_budget is not None else None,
        "currency": ops.currency,
        "last_discovery": {
            "status": last.status,
            "at": _fmt(last.started_at, tz),
            "reason": str((last.summary or {}).get("reason", ""))[:200],
        }
        if last
        else None,
        "paused": {
            "discovery": ops.pause_discovery,
            "outbound": ops.pause_outbound,
            "all": ops.pause_all_processing,
        },
    }


def today_text(f: dict[str, Any]) -> str:
    lines = [
        f"ملخص اليوم ({f['date']}) — الوضع: {'فعلي' if f['mode'] == 'live' else 'تجريبي (يدوي)'}",
        f"• بانتظار اعتمادك: {f['pending_approvals']}",
        f"• فرص عولجت اليوم: {f['qualified_today']} من {f['daily_quota']} · مرشحون اكتُشفوا: {f['discovered_today']}",
        f"• الإنفاق: اليوم {f['spent_today']} · الشهر {f['spent_month']} {f['currency']}"
        + (
            f" (الميزانية اليومية {f['daily_budget']})"
            if f["daily_budget"]
            else " (لا ميزانية محددة)"
        ),
    ]
    if f["last_discovery"]:
        d = f["last_discovery"]
        lines.append(
            f"• آخر دورة اكتشاف: {d['status']} في {d['at']}"
            + (f" — {d['reason']}" if d["reason"] else "")
        )
    else:
        lines.append("• لم تعمل دورة اكتشاف بعد")
    if f["unknown_delivery"]:
        lines.append(f"⚠️ {f['unknown_delivery']} رسالة بتسليم غير معروف تحتاج مصالحة في اللوحة")
    if f["needs_linking"]:
        lines.append(f"⚠️ {f['needs_linking']} رد وارد يحتاج ربطًا يدويًا")
    paused = [k for k, v in f["paused"].items() if v]
    if paused:
        lines.append(
            "⏸ موقوف: "
            + "، ".join(
                {"discovery": "الاكتشاف", "outbound": "الإرسال", "all": "كل المعالجة"}[k]
                for k in paused
            )
        )
    return "\n".join(lines)


async def pending_drafts(
    db: AsyncSession, workspace_id: uuid.UUID, limit: int = 10
) -> list[dict[str, Any]]:
    _, tz = await _ws(db, workspace_id)
    rows = (
        await db.execute(
            select(Draft, Company.display_name, Product.name, Opportunity.score)
            .join(Opportunity, Opportunity.id == Draft.opportunity_id)
            .join(Company, Company.id == Opportunity.company_id)
            .join(Product, Product.id == Opportunity.product_id)
            .where(Draft.workspace_id == workspace_id, Draft.status == "pending_review")
            .order_by(Draft.created_at)
            .limit(limit)
        )
    ).tuples()
    return [
        {
            "company": company,
            "product": product,
            "score": score,
            "subject": d.subject,
            "channel": d.channel,
            "kind": d.kind,
            "since": _fmt(d.created_at, tz),
        }
        for d, company, product, score in rows
    ]


def pending_text(items: list[dict[str, Any]], base_url: str) -> str:
    if not items:
        return "لا مسودات بانتظار اعتمادك الآن."
    lines = [f"بانتظار اعتمادك ({len(items)}):"]
    for i, it in enumerate(items, 1):
        kind = "رد" if it["kind"] == "reply" else "تواصل أول"
        score = f" · {it['score']}/100" if it["score"] is not None else ""
        lines.append(f"{i}. {it['company']} · {it['product']}{score} · {kind} · منذ {it['since']}")
    if base_url.startswith("https://"):
        lines.append(f"\nللمراجعة: {base_url.rstrip('/')}/approvals")
    return "\n".join(lines)


async def _mentioned_companies(
    db: AsyncSession, workspace_id: uuid.UUID, question: str, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """الشركات التي يذكرها السؤال بالاسم (مطابقة الاسم المطبع أو كلمتين مميزتين منه)."""
    q_norm = normalize_name(question)
    q_words = {w for w in q_norm.split() if len(w) > 2}
    if not q_words:
        return []
    candidates = (
        await db.execute(
            select(Company)
            .where(Company.workspace_id == workspace_id)
            .order_by(Company.updated_at.desc())
            .limit(1000)
        )
    ).scalars()
    scored: list[tuple[int, Company]] = []
    for c in candidates:
        words = {w for w in (c.normalized_name or "").split() if len(w) > 2}
        common = len(words & q_words)
        if (
            (c.normalized_name and c.normalized_name in q_norm)
            or common >= 2
            or (common == 1 and len(words) == 1)
        ):
            scored.append(
                (common + (5 if c.normalized_name and c.normalized_name in q_norm else 0), c)
            )
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _, c in scored[:2]:
        opps = (
            await db.execute(
                select(Opportunity, Product.name)
                .join(Product, Product.id == Opportunity.product_id)
                .where(Opportunity.company_id == c.id)
            )
        ).tuples()
        drafts = (
            await db.execute(
                select(Draft.status, Draft.subject, Draft.created_at)
                .join(Opportunity, Opportunity.id == Draft.opportunity_id)
                .where(Opportunity.company_id == c.id)
                .order_by(Draft.created_at.desc())
                .limit(5)
            )
        ).all()
        msgs = (
            await db.execute(
                select(
                    Message.direction,
                    Message.classification,
                    Message.classification_detail,
                    Message.sent_or_received_at,
                )
                .where(Message.company_id == c.id)
                .order_by(Message.sent_or_received_at.desc())
                .limit(5)
            )
        ).all()
        out.append(
            {
                "name": c.display_name,
                "domain": c.domain,
                "status": c.status,
                "opportunities": [
                    {
                        "product": pname,
                        "status": STATUS_AR.get(o.status, o.status),
                        "score": o.score,
                        "reason_if_disqualified": o.disqualify_reason,
                        "facts": [str(f.get("claim", ""))[:200] for f in (o.facts or [])[:3]],
                        "updated": _fmt(o.updated_at, tz),
                    }
                    for o, pname in opps
                ],
                "drafts": [
                    {"status": s, "subject": subj, "at": _fmt(at, tz)} for s, subj, at in drafts
                ],
                "messages": [
                    {
                        "direction": d,
                        "classification": cls,
                        "summary": str((detail or {}).get("summary", ""))[:200],
                        "at": _fmt(at, tz),
                    }
                    for d, cls, detail, at in msgs
                ],
            }
        )
    return out


async def build_context(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, question: str
) -> dict[str, Any]:
    """لقطة قراءة فقط ومحدودة الحجم، بلا قيم جهات اتصال."""
    _, tz = await _ws(db, workspace_id)
    opps = (
        await db.execute(
            select(Opportunity, Company.display_name, Product.name)
            .join(Company, Company.id == Opportunity.company_id)
            .join(Product, Product.id == Opportunity.product_id)
            .where(Opportunity.workspace_id == workspace_id)
            .order_by(Opportunity.updated_at.desc())
            .limit(25)
        )
    ).tuples()
    products = (
        await db.execute(
            select(Product.name, Product.status, Product.priority).where(
                Product.workspace_id == workspace_id, Product.status != "archived"
            )
        )
    ).all()
    counts = dict(
        (
            await db.execute(
                select(Opportunity.status, func.count())
                .where(Opportunity.workspace_id == workspace_id)
                .group_by(Opportunity.status)
            )
        )
        .tuples()
        .all()
    )
    runs = (
        await db.execute(
            select(Run.kind, Run.status, Run.started_at, Run.summary)
            .where(Run.workspace_id == workspace_id)
            .order_by(Run.started_at.desc())
            .limit(6)
        )
    ).all()
    inbound = (
        await db.execute(
            select(
                Company.display_name,
                Message.classification,
                Message.classification_detail,
                Message.sent_or_received_at,
            )
            .join(Company, Company.id == Message.company_id, isouter=True)
            .where(Message.workspace_id == workspace_id, Message.direction == "inbound")
            .order_by(Message.sent_or_received_at.desc())
            .limit(8)
        )
    ).all()
    return {
        "now": datetime.now(UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "today": await today_facts(db, settings, workspace_id),
        "products": [{"name": n, "status": s, "priority": p} for n, s, p in products],
        "opportunity_counts": {STATUS_AR.get(k, k): v for k, v in counts.items()},
        "opportunities": [
            {
                "company": cname,
                "product": pname,
                "status": STATUS_AR.get(o.status, o.status),
                "score": o.score,
                "updated": _fmt(o.updated_at, tz),
            }
            for o, cname, pname in opps
        ],
        "pending_drafts": await pending_drafts(db, workspace_id),
        "recent_runs": [
            {
                "kind": k,
                "status": s,
                "at": _fmt(at, tz),
                "reason": str((summ or {}).get("reason", ""))[:160],
            }
            for k, s, at, summ in runs
        ],
        "recent_replies": [
            {
                "company": cname or "غير مربوطة",
                "classification": cls,
                "summary": str((detail or {}).get("summary", ""))[:160],
                "at": _fmt(at, tz),
            }
            for cname, cls, detail, at in inbound
        ],
        "companies_mentioned": await _mentioned_companies(db, workspace_id, question, tz),
    }


async def answer_question(deps: Deps, workspace_id: uuid.UUID, question: str) -> str:
    async with deps.sm() as db:
        ctx = await build_context(db, deps.settings, workspace_id, question)
    try:
        out, _ = await deps.gateway(workspace_id).complete(
            role="writer",
            system=ASSISTANT_SYSTEM,
            user="سؤال عضو الفريق:\n" + question.strip()[:1000] + "\n\n" + context_block(ctx),
            output_model=AssistantAnswer,
            schema_name="assistant_answer",
            category="assistant",
            counter=CallCounter(2),
            max_output_tokens=1200,
            prompt_version=PROMPT_VERSION,
        )
    except budget.BudgetBlocked as exc:
        return f"لا أستطيع استدعاء النموذج الآن: {exc}\nالأوامر /today و/pending تعمل دون نموذج."
    except ModelNotReady as exc:
        return f"النموذج غير جاهز: {exc}\nأكمل الإعدادات ← النماذج. الأوامر /today و/pending تعمل دون نموذج."
    except (ProviderError, ModelOutputInvalid, CallLimitReached, AppError) as exc:
        return f"تعذر الحصول على إجابة الآن ({exc}). جرّب لاحقًا أو استخدم /today."
    return out.answer
