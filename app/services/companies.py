"""مطابقة الشركات وإنشاؤها. الدمج التلقائي يحدث فقط بمعرف قوي يخص شركة واحدة؛ غير ذلك مراجعة بشرية."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import data_hash
from app.config import Settings
from app.db.models import (
    Company,
    CompanyDuplicateCandidate,
    CompanyIdentifier,
    CompanySourceLink,
    Contact,
    Suppression,
)
from app.services.normalize import Identifier


@dataclass
class CompanyDraft:
    display_name: str
    normalized_name: str
    identifiers: list[Identifier] = field(default_factory=list)
    domain: str | None = None
    email_domains: list[str] = field(default_factory=list)
    sector: str | None = None
    region: str | None = None
    city: str | None = None
    country: str | None = None
    segment_id: uuid.UUID | None = None
    notes: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[tuple[str, bool]] = field(default_factory=list)
    contact_name: str | None = None
    contact_role: str | None = None
    source_record_ref: str | None = None
    source_url: str | None = None

    @property
    def strong(self) -> list[Identifier]:
        return [i for i in self.identifiers if i.strength == "strong"]


@dataclass
class MatchResult:
    action: str  # create | link | review | skip_conflict
    company_id: uuid.UUID | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "company_id": str(self.company_id) if self.company_id else None,
            "candidates": self.candidates,
            "reason": self.reason,
        }


async def match_company(
    db: AsyncSession, workspace_id: uuid.UUID, draft: CompanyDraft
) -> MatchResult:
    strong = draft.strong
    if strong:
        rows = (
            await db.execute(
                select(CompanyIdentifier.company_id, CompanyIdentifier.kind).where(
                    CompanyIdentifier.workspace_id == workspace_id,
                    CompanyIdentifier.strength == "strong",
                    tuple_(CompanyIdentifier.kind, CompanyIdentifier.normalized_value).in_(
                        [(i.kind, i.value) for i in strong]
                    ),
                )
            )
        ).all()
        ids = {r.company_id for r in rows}
        if len(ids) == 1:
            kinds = sorted({r.kind for r in rows})
            return MatchResult(
                "link", next(iter(ids)), reason="تطابق معرف قوي: " + "، ".join(kinds)
            )
        if len(ids) > 1:
            return MatchResult(
                "skip_conflict",
                candidates=[
                    {"company_id": str(i), "reasons": ["معرف قوي يخص شركة أخرى"]} for i in ids
                ],
                reason="المعرفات القوية تشير إلى أكثر من شركة؛ لا دمج تلقائي",
            )

    reasons: dict[uuid.UUID, list[str]] = {}
    weak_conditions = []
    for phone, _ok in draft.phones:
        weak_conditions.append(
            and_(CompanyIdentifier.kind == "phone", CompanyIdentifier.normalized_value == phone)
        )
    if draft.normalized_name:
        weak_conditions.append(
            and_(
                CompanyIdentifier.kind == "name",
                CompanyIdentifier.normalized_value == draft.normalized_name,
            )
        )
    for dom in draft.email_domains:
        weak_conditions.append(
            and_(CompanyIdentifier.kind == "domain", CompanyIdentifier.normalized_value == dom)
        )
    if weak_conditions:
        rows = (
            await db.execute(
                select(CompanyIdentifier.company_id, CompanyIdentifier.kind).where(
                    CompanyIdentifier.workspace_id == workspace_id, or_(*weak_conditions)
                )
            )
        ).all()
        labels = {
            "phone": "رقم هاتف مشترك",
            "name": "اسم مطابق بعد التطبيع",
            "domain": "نطاق البريد يطابق موقع شركة",
        }
        for r in rows:
            reasons.setdefault(r.company_id, [])
            label = labels.get(r.kind, r.kind)
            if label not in reasons[r.company_id]:
                reasons[r.company_id].append(label)
    if reasons:
        return MatchResult(
            "review",
            candidates=[{"company_id": str(cid), "reasons": rs} for cid, rs in reasons.items()],
            reason="تطابق ضعيف فقط؛ تُنشأ الشركة بحالة «تحتاج مراجعة» دون دمج",
        )
    return MatchResult("create", reason="لا تطابق")


async def suppressed(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, draft: CompanyDraft
) -> bool:
    hashes: list[tuple[str, bytes]] = []
    for ident in draft.strong:
        if ident.kind in ("domain", "platform_account"):
            hashes.append(("domain", data_hash(settings, "domain", ident.value)))
    for email in draft.emails:
        hashes.append(("email", data_hash(settings, "email", email.lower())))
    for phone, _ok in draft.phones:
        hashes.append(("phone", data_hash(settings, "phone", phone)))
    if not hashes:
        return False
    found = (
        await db.execute(
            select(func.count())
            .select_from(Suppression)
            .where(
                Suppression.workspace_id == workspace_id,
                tuple_(Suppression.scope, Suppression.target_hash).in_(hashes),
            )
        )
    ).scalar_one()
    return bool(found)


async def company_suppressed(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID, company_id: uuid.UUID
) -> bool:
    h = data_hash(settings, "company", str(company_id))
    q = (
        select(func.count())
        .select_from(Suppression)
        .where(
            Suppression.workspace_id == workspace_id,
            Suppression.scope == "company",
            Suppression.target_hash == h,
        )
    )
    return bool((await db.execute(q)).scalar_one())


async def _add_identifiers(
    db: AsyncSession, workspace_id: uuid.UUID, company_id: uuid.UUID, draft: CompanyDraft
) -> None:
    idents = list(draft.identifiers)
    idents.append(Identifier("name", draft.normalized_name, "weak"))
    idents.extend(Identifier("phone", p, "weak") for p, _ok in draft.phones)
    for ident in idents:
        if not ident.value:
            continue
        stmt = insert(CompanyIdentifier).values(
            workspace_id=workspace_id,
            company_id=company_id,
            kind=ident.kind,
            normalized_value=ident.value,
            strength=ident.strength,
        )
        # معرف قوي مسجل لشركة أخرى لا يُنقل؛ التعارض عولج في المطابقة.
        await db.execute(stmt.on_conflict_do_nothing())


async def _add_contacts(
    db: AsyncSession,
    settings: Settings,
    workspace_id: uuid.UUID,
    company_id: uuid.UUID,
    draft: CompanyDraft,
    provenance: dict[str, Any],
) -> int:
    added = 0
    items: list[tuple[str, str, bool]] = [("email", e, True) for e in draft.emails]
    items += [("phone", p, ok) for p, ok in draft.phones]
    for channel, value, normalized in items:
        key = value.lower() if channel == "email" else value
        stmt = insert(Contact).values(
            workspace_id=workspace_id,
            company_id=company_id,
            channel=channel,
            value=value.removeprefix("raw:"),
            value_hash=data_hash(settings, channel, key),
            normalized=normalized,
            professional_name=draft.contact_name,
            role_title=draft.contact_role,
            provenance=provenance,
        )
        res = await db.execute(stmt.on_conflict_do_nothing())
        added += res.rowcount or 0  # type: ignore[attr-defined]
    return added


async def apply_draft(
    db: AsyncSession,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_id: str,
    draft: CompanyDraft,
    import_batch_id: uuid.UUID | None,
    is_demo: bool = False,
) -> tuple[MatchResult, uuid.UUID | None]:
    """يطبق مسودة شركة: ربط بشركة موجودة، أو إنشاء (مع مرشحي تكرار عند الحاجة)."""
    if await suppressed(db, settings, workspace_id, draft):
        return MatchResult("skip_suppressed", reason="الجهة في سجل منع التواصل"), None
    match = await match_company(db, workspace_id, draft)
    if match.action == "skip_conflict":
        return match, None
    if match.action == "link":
        assert match.company_id
        if await company_suppressed(db, settings, workspace_id, match.company_id):
            return MatchResult("skip_suppressed", reason="الجهة في سجل منع التواصل"), None
        company_id = match.company_id
    else:
        company = Company(
            workspace_id=workspace_id,
            display_name=draft.display_name,
            normalized_name=draft.normalized_name,
            domain=draft.domain,
            sector=draft.sector,
            region=draft.region,
            city=draft.city,
            country=draft.country,
            segment_id=draft.segment_id,
            notes=draft.notes,
            status="needs_review" if match.action == "review" else "active",
            is_demo_data=is_demo,
            created_by=actor_id,
        )
        db.add(company)
        await db.flush()
        company_id = company.id
        for cand in match.candidates:
            await db.execute(
                insert(CompanyDuplicateCandidate)
                .values(
                    workspace_id=workspace_id,
                    company_id=company_id,
                    candidate_company_id=uuid.UUID(cand["company_id"]),
                    reasons=cand["reasons"],
                )
                .on_conflict_do_nothing()
            )
    await _add_identifiers(db, workspace_id, company_id, draft)
    provenance = {
        "source_id": str(source_id),
        "import_batch_id": str(import_batch_id) if import_batch_id else None,
    }
    await _add_contacts(db, settings, workspace_id, company_id, draft, provenance)
    ref = draft.source_record_ref or (f"batch:{import_batch_id}" if import_batch_id else "manual")
    now = datetime.now(UTC)
    await db.execute(
        insert(CompanySourceLink)
        .values(
            workspace_id=workspace_id,
            company_id=company_id,
            source_id=source_id,
            source_record_ref=ref[:500],
            url=draft.source_url,
            import_batch_id=import_batch_id,
        )
        .on_conflict_do_update(
            index_elements=[
                CompanySourceLink.company_id,
                CompanySourceLink.source_id,
                CompanySourceLink.source_record_ref,
            ],
            set_={"last_seen_at": now},
        )
    )
    return match, company_id


async def add_manual_contact(
    db: AsyncSession,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    actor_id: str,
    company_id: uuid.UUID,
    channel: str,
    value: str,
    name: str = "",
    role: str = "",
    phone_region: str | None = None,
) -> Contact:
    """جهة اتصال يضيفها المستخدم يدويًا لشركة موجودة. أهليتها تبدأ «غير معروفة» حتى يوثّق سندها."""
    from app.api.errors import Conflict, InvalidInput
    from app.services import audit
    from app.services.normalize import normalize_email, normalize_phone

    try:
        if channel == "email":
            normalized, ok = normalize_email(value), True
            key = normalized.lower()
        elif channel == "phone":
            normalized, ok = normalize_phone(value, phone_region)
            key = normalized
        else:
            raise InvalidInput("القناة يجب أن تكون بريدًا أو هاتفًا")
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc
    h = data_hash(settings, channel, key)
    suppressed = (
        await db.execute(
            select(func.count())
            .select_from(Suppression)
            .where(
                Suppression.workspace_id == workspace_id,
                Suppression.scope == channel,
                Suppression.target_hash == h,
            )
        )
    ).scalar_one()
    if suppressed or await company_suppressed(db, settings, workspace_id, company_id):
        raise Conflict("هذه الجهة أو القيمة في سجل منع التواصل", code="suppressed")
    existing = (
        await db.execute(
            select(Contact).where(
                Contact.workspace_id == workspace_id,
                Contact.company_id == company_id,
                Contact.channel == channel,
                Contact.value_hash == h,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    contact = Contact(
        workspace_id=workspace_id,
        company_id=company_id,
        channel=channel,
        value=normalized.removeprefix("raw:"),
        value_hash=h,
        normalized=ok,
        professional_name=name.strip()[:200] or None,
        role_title=role.strip()[:200] or None,
        provenance={"source": "manual", "actor": actor_id},
    )
    db.add(contact)
    await db.flush()
    audit.record(
        db,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action="contact.add_manual",
        entity_type="contact",
        entity_id=contact.id,
        change={"channel": channel},
    )
    return contact
