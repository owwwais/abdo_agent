"""استيراد يدوي وCSV: معاينة بأخطاء الصفوف دون إسقاط الملف كله، ثم اعتماد idempotent.

بيانات الصفوف الخام تُحذف بعد الاعتماد أو الإلغاء؛ يبقى الملخص والإجراء لكل صف.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Conflict, InvalidInput
from app.auth.sessions import Principal
from app.config import Settings
from app.connectors.netguard import BlockedUrl, check_url
from app.db.models import Company, ImportBatch, ImportRow, Segment, Source, Workspace
from app.services import audit
from app.services.common import Input, Text, get_scoped
from app.services.companies import CompanyDraft, apply_draft, match_company, suppressed
from app.services.normalize import (
    email_domain,
    normalize_country,
    normalize_cr,
    normalize_email,
    normalize_name,
    normalize_phone,
    website_identifier,
)

MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 2000
PREVIEW_TTL = timedelta(hours=24)

# أسماء الأعمدة المقبولة (عربي/إنجليزي) → الحقل الداخلي.
COLUMN_ALIASES: dict[str, str] = {}
for _field, _names in {
    "name": [
        "name",
        "company",
        "company_name",
        "الاسم",
        "اسم الجهة",
        "اسم المنشأة",
        "الجهة",
        "المنشأة",
    ],
    "website": [
        "website",
        "domain",
        "url",
        "site",
        "الموقع",
        "الموقع الإلكتروني",
        "النطاق",
        "رابط الموقع",
    ],
    "sector": ["sector", "industry", "activity", "النشاط", "القطاع"],
    "segment": ["segment", "category", "الفئة", "الشريحة"],
    "region": ["region", "المنطقة"],
    "city": ["city", "المدينة"],
    "country": ["country", "الدولة"],
    "phone": ["phone", "mobile", "tel", "الهاتف", "الجوال", "رقم الهاتف", "رقم الجوال"],
    "email": ["email", "e-mail", "mail", "البريد", "البريد الإلكتروني", "الإيميل"],
    "contact_name": ["contact_name", "contact", "person", "اسم المسؤول", "المسؤول", "جهة الاتصال"],
    "contact_role": ["contact_role", "title", "role", "المنصب", "الوظيفة"],
    "cr_number": [
        "cr_number",
        "cr",
        "unified_number",
        "السجل التجاري",
        "رقم السجل",
        "الرقم الموحد",
    ],
    "notes": ["notes", "note", "ملاحظات", "ملاحظة"],
    "source_url": ["source_url", "reference", "رابط المصدر", "المرجع"],
}.items():
    for _n in _names:
        COLUMN_ALIASES[_n.strip().lower()] = _field

FIELD_LIMITS = {
    "name": 300,
    "website": 500,
    "sector": 200,
    "segment": 200,
    "region": 200,
    "city": 200,
    "country": 60,
    "phone": 40,
    "email": 320,
    "contact_name": 200,
    "contact_role": 200,
    "cr_number": 20,
    "notes": 2000,
    "source_url": 2000,
}


class ManualEntry(Input):
    name: Text = Field(min_length=1, max_length=300)
    website: Text = Field(default="", max_length=500)
    sector: Text = Field(default="", max_length=200)
    segment: Text = Field(default="", max_length=200)
    region: Text = Field(default="", max_length=200)
    city: Text = Field(default="", max_length=200)
    country: Text = Field(default="", max_length=60)
    phone: Text = Field(default="", max_length=40)
    email: Text = Field(default="", max_length=320)
    contact_name: Text = Field(default="", max_length=200)
    contact_role: Text = Field(default="", max_length=200)
    cr_number: Text = Field(default="", max_length=20)
    notes: Text = Field(default="", max_length=2000)
    source_url: Text = Field(default="", max_length=2000)


@dataclass
class ParsedRow:
    row_number: int
    data: dict[str, str]
    errors: list[dict[str, str]]
    draft: CompanyDraft | None


def decode_csv(raw: bytes) -> str:
    if len(raw) > MAX_FILE_BYTES:
        raise InvalidInput("حجم الملف أكبر من 2 ميغابايت", code="file_too_large")
    for enc in ("utf-8-sig", "cp1256"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise InvalidInput("تعذر قراءة ترميز الملف؛ احفظه بصيغة CSV UTF-8", code="bad_encoding")


def read_rows(content: str) -> tuple[list[dict[str, str]], list[str]]:
    """يعيد الصفوف بأسماء حقول داخلية، وقائمة الأعمدة المتجاهلة."""
    sample = content[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(content), dialect)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise InvalidInput("الملف فارغ", code="empty_file") from exc
    mapping: list[str | None] = []
    ignored: list[str] = []
    for col in header:
        key = COLUMN_ALIASES.get(col.strip().lower())
        if key is None or key in mapping:
            ignored.append(col.strip())
            key = None
        mapping.append(key)
    if "name" not in mapping:
        raise InvalidInput(
            "لا يوجد عمود للاسم. الأعمدة المقبولة للاسم: name أو «الاسم» أو «اسم الجهة»",
            code="missing_name_column",
        )
    rows: list[dict[str, str]] = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        if len(rows) >= MAX_ROWS:
            raise InvalidInput(f"عدد الصفوف أكبر من {MAX_ROWS}", code="too_many_rows")
        row = {
            k: (values[i].strip() if i < len(values) else "") for i, k in enumerate(mapping) if k
        }
        rows.append(row)
    return rows, [c for c in ignored if c]


def build_draft(
    data: dict[str, str], segments_by_name: dict[str, uuid.UUID], default_region: str | None
) -> tuple[CompanyDraft | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []

    def err(field: str, message: str) -> None:
        errors.append({"field": field, "message": message})

    for field, limit in FIELD_LIMITS.items():
        if len(data.get(field, "")) > limit:
            err(field, f"النص أطول من {limit} حرف")
    name = data.get("name", "").strip()
    if not name:
        err("name", "الاسم مطلوب")
    normalized = normalize_name(name)
    if name and not normalized:
        err("name", "الاسم لا يحتوي حروفًا صالحة")

    draft = CompanyDraft(display_name=name[:300], normalized_name=normalized)
    try:
        ident, domain = website_identifier(data.get("website", ""))
        if ident:
            draft.identifiers.append(ident)
            draft.domain = domain
    except ValueError as exc:
        err("website", str(exc))
    try:
        cr = normalize_cr(data.get("cr_number", ""))
        if cr:
            draft.identifiers.append(cr)
    except ValueError as exc:
        err("cr_number", str(exc))
    try:
        draft.country = normalize_country(data.get("country", ""))
    except ValueError as exc:
        err("country", str(exc))
    if data.get("phone"):
        try:
            draft.phones.append(normalize_phone(data["phone"], draft.country or default_region))
        except ValueError as exc:
            err("phone", str(exc))
    if data.get("email"):
        try:
            email = normalize_email(data["email"])
            draft.emails.append(email)
            dom = email_domain(email)
            if dom and dom != draft.domain:
                draft.email_domains.append(dom)
        except ValueError as exc:
            err("email", str(exc))
    seg_name = data.get("segment", "").strip()
    if seg_name:
        seg_id = segments_by_name.get(seg_name)
        if seg_id is None:
            err("segment", f"الفئة «{seg_name}» غير موجودة")
        draft.segment_id = seg_id
    source_url = data.get("source_url", "").strip()
    if source_url:
        try:
            draft.source_url = check_url(source_url).url
            draft.source_record_ref = draft.source_url
        except BlockedUrl as exc:
            err("source_url", exc.message)
    draft.sector = data.get("sector") or None
    draft.region = data.get("region") or None
    draft.city = data.get("city") or None
    draft.notes = data.get("notes", "")
    draft.contact_name = data.get("contact_name") or None
    draft.contact_role = data.get("contact_role") or None
    return (None if errors else draft), errors


def _in_file_key(draft: CompanyDraft) -> list[str]:
    return [f"{i.kind}:{i.value}" for i in draft.strong]


async def _import_source(db: AsyncSession, p: Principal, source_id: uuid.UUID) -> Source:
    src = await get_scoped(db, Source, p.workspace_id, source_id)
    if src.status != "active":
        raise Conflict(
            "الاستيراد يحتاج مصدرًا نشطًا أكد المالك سياسة استخدامه وتخزينه", code="source_not_active"
        )
    return src


async def _segments_map(db: AsyncSession, workspace_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await db.execute(
        select(Segment.name, Segment.id).where(
            Segment.workspace_id == workspace_id, Segment.status == "active"
        )
    )
    return {name: sid for name, sid in rows.tuples()}


async def create_preview(
    db: AsyncSession,
    p: Principal,
    settings: Settings,
    *,
    source_id: uuid.UUID,
    rows: list[dict[str, str]],
    kind: str,
    filename: str | None = None,
    ignored_columns: list[str] | None = None,
) -> ImportBatch:
    await _import_source(db, p, source_id)
    ws = await db.get(Workspace, p.workspace_id)
    region = ws.default_phone_region if ws else None
    segments = await _segments_map(db, p.workspace_id)
    batch = ImportBatch(
        workspace_id=p.workspace_id,
        source_id=source_id,
        kind=kind,
        filename=(filename or "")[:300] or None,
        status="preview",
        row_count=len(rows),
        created_by=p.actor_id,
        expires_at=datetime.now(UTC) + PREVIEW_TTL,
        summary={"ignored_columns": ignored_columns or []},
    )
    db.add(batch)
    await db.flush()
    seen: dict[str, int] = {}
    counts: dict[str, int] = {}
    for idx, data in enumerate(rows, start=2 if kind == "csv" else 1):
        draft, errors = build_draft(data, segments, region)
        match: dict[str, Any] = {}
        if errors or draft is None:
            action = "skip_error"
        else:
            dup = next((seen[k] for k in _in_file_key(draft) if k in seen), None)
            if dup is not None:
                action = "skip_duplicate_in_file"
                match = {"reason": f"مكرر لصف {dup} في الملف نفسه"}
            elif await suppressed(db, settings, p.workspace_id, draft):
                action = "skip_suppressed"
                match = {"reason": "الجهة في سجل منع التواصل"}
            else:
                result = await match_company(db, p.workspace_id, draft)
                action = result.action
                match = result.to_dict()
            for k in _in_file_key(draft):
                seen.setdefault(k, idx)
        counts[action] = counts.get(action, 0) + 1
        db.add(
            ImportRow(
                workspace_id=p.workspace_id,
                batch_id=batch.id,
                row_number=idx,
                # الصفوف المرفوضة لا تحفظ بياناتها؛ تكفي رسالة الخطأ ورقم الصف والاسم.
                data=data if action not in ("skip_error", "skip_suppressed") else None,
                display_name=(data.get("name") or "")[:300],
                errors=errors,
                match=match,
                action=action,
            )
        )
    batch.error_count = counts.get("skip_error", 0)
    batch.summary = {**batch.summary, "counts": counts}
    await db.flush()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="import.preview",
        entity_type="import_batch",
        entity_id=batch.id,
        change={"kind": kind, "rows": len(rows), "counts": counts, "source_id": str(source_id)},
    )
    return batch


async def commit_batch(
    db: AsyncSession, p: Principal, settings: Settings, batch_id: uuid.UUID
) -> ImportBatch:
    """اعتماد idempotent: الاعتماد الثاني يعيد النتيجة نفسها دون أثر إضافي."""
    batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id, lock=True)
    if batch.status == "committed":
        return batch
    if batch.status != "preview":
        raise Conflict("الدفعة ملغاة", code="batch_canceled")
    if batch.expires_at < datetime.now(UTC):
        raise Conflict("انتهت صلاحية المعاينة؛ ارفع الملف مجددًا", code="preview_expired")
    src = await _import_source(db, p, batch.source_id)
    # يمنع سباق دفعتين تنشئان المعرف القوي نفسه لشركتين مختلفتين.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"import:{p.workspace_id}"}
    )
    ws = await db.get(Workspace, p.workspace_id)
    region = ws.default_phone_region if ws else None
    segments = await _segments_map(db, p.workspace_id)
    rows = list(
        (
            await db.execute(
                select(ImportRow)
                .where(ImportRow.batch_id == batch.id)
                .order_by(ImportRow.row_number)
            )
        ).scalars()
    )
    counts: dict[str, int] = {}
    for row in rows:
        if (
            row.action in ("skip_error", "skip_duplicate_in_file", "skip_suppressed")
            or row.data is None
        ):
            counts[row.action] = counts.get(row.action, 0) + 1
            continue
        draft, errors = build_draft(row.data, segments, region)
        if draft is None:
            row.action, row.errors = "skip_error", errors
        else:
            # تُعاد المطابقة وقت الاعتماد لأن القاعدة قد تغيرت بعد المعاينة.
            result, company_id = await apply_draft(
                db,
                settings,
                workspace_id=p.workspace_id,
                source_id=src.id,
                actor_id=p.actor_id,
                draft=draft,
                import_batch_id=batch.id,
                is_demo=src.is_demo_data,
            )
            row.action = result.action
            row.match = result.to_dict()
            row.result_company_id = company_id
        counts[row.action] = counts.get(row.action, 0) + 1
    await db.execute(update(ImportRow).where(ImportRow.batch_id == batch.id).values(data=None))
    batch.status = "committed"
    batch.committed_at = datetime.now(UTC)
    batch.summary = {**batch.summary, "committed_counts": counts}
    await db.flush()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="import.commit",
        entity_type="import_batch",
        entity_id=batch.id,
        change={"counts": counts},
    )
    return batch


async def cancel_batch(db: AsyncSession, p: Principal, batch_id: uuid.UUID) -> ImportBatch:
    batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id, lock=True)
    if batch.status == "committed":
        raise Conflict("لا يمكن إلغاء دفعة معتمدة", code="batch_committed")
    batch.status = "canceled"
    await db.execute(update(ImportRow).where(ImportRow.batch_id == batch.id).values(data=None))
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="import.cancel",
        entity_type="import_batch",
        entity_id=batch.id,
    )
    return batch


async def batch_rows(db: AsyncSession, p: Principal, batch_id: uuid.UUID) -> list[ImportRow]:
    await get_scoped(db, ImportBatch, p.workspace_id, batch_id)
    q = select(ImportRow).where(
        ImportRow.batch_id == batch_id, ImportRow.workspace_id == p.workspace_id
    )
    return list((await db.execute(q.order_by(ImportRow.row_number))).scalars())


async def recent_batches(db: AsyncSession, p: Principal, limit: int = 20) -> list[ImportBatch]:
    q = (
        select(ImportBatch)
        .where(ImportBatch.workspace_id == p.workspace_id)
        .order_by(ImportBatch.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(q)).scalars())


async def count_companies(db: AsyncSession, p: Principal) -> int:
    q = select(func.count()).select_from(Company).where(Company.workspace_id == p.workspace_id)
    return int((await db.execute(q)).scalar_one())
