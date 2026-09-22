"""المصادر: إعداد، فحص عينة عبر مهمة في الطابور، ثم تفعيل بتأكيد المالك لسياسة الاستخدام والتخزين.

الحالات: new → validating → sample_ready → active، أو needs_setup / restricted / failed / paused.
نجاح القراءة تقنيًا لا يساوي السماح بالاستخدام؛ التفعيل خطوة بشرية منفصلة.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Conflict, InvalidInput, VersionConflict
from app.auth.sessions import Principal
from app.config import Settings
from app.connectors.base import SampleResult, SourceConfig
from app.connectors.netguard import BlockedUrl, check_url, host_allowed, path_allowed
from app.connectors.registry import SOURCE_KINDS, connector_key_for
from app.db.models import Job, Segment, Source, SourceCheck, SourceSegment
from app.jobs import queue
from app.services import audit
from app.services.common import Input, Text, TextList, get_scoped, require_owner

# نسخة قائمة التأكيدات التي يقرها المالك عند التفعيل. رفعها يلزم إعادة تأكيد المصادر.
SOURCE_POLICY_VERSION = 1
SOURCE_POLICY_ITEMS = (
    "راجعتُ شروط استخدام المصدر وسياساته المعلنة، والاستخدام المقصود مسموح (robots.txt وحده ليس ترخيصًا)",
    "لا يتطلب المصدر تسجيل دخول أو تجاوز حماية أو CAPTCHA، ولا يجمع بيانات مرضى أو نزلاء أو أفراد خاصين",
    "مدة الاحتفاظ وسياسة حفظ المحتوى الخام المحددتان مناسبتان لهذا المصدر",
)
SOURCE_SAMPLE_JOB = "source_sample"
CHECK_RETENTION_DAYS = 30

_CRED_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_HOST_PATTERN_RE = re.compile(r"^(\*\.)?[a-z0-9.-]+$")
# تغيير هذه الحقول يمس الوصول أو التخزين، فيُبطل الفحص والتفعيل السابقين.
_CONFIG_FIELDS = {
    "url",
    "kind",
    "allowed_hosts",
    "allowed_paths",
    "max_pages",
    "max_records",
    "max_requests",
    "timeout_seconds",
    "credential_ref",
    "store_raw",
    "retention_days",
}

SourceKind = Literal["manual", "html", "rss", "portal", "web_search", "linkedin", "google_maps"]


def _source_url(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    try:
        return check_url(v.strip()).url
    except BlockedUrl as exc:
        raise ValueError(exc.message) from exc


def _credential_ref(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    v = v.strip()
    if not _CRED_REF_RE.match(v):
        raise ValueError(
            "مرجع السر اسم متغير بيئة بأحرف كبيرة (مثل SOURCE_X_API_KEY)، وليس المفتاح نفسه"
        )
    return v


def _hosts(v: list[str]) -> list[str]:
    out = []
    for h in v:
        h = h.lower().strip().rstrip(".")
        if not _HOST_PATTERN_RE.match(h) or "." not in h.removeprefix("*."):
            raise ValueError(f"نطاق غير صالح: {h}")
        try:
            check_url(f"https://{h.removeprefix('*.')}/")
        except BlockedUrl as exc:
            raise ValueError(f"{h}: {exc.message}") from exc
        out.append(h)
    return out


def _paths(v: list[str]) -> list[str]:
    for path in v:
        if not path.startswith("/"):
            raise ValueError("المسار المسموح يبدأ بـ / (مثل /directory/)")
    return v


SourceUrl = Annotated[str | None, Field(max_length=2000), AfterValidator(_source_url)]
CredentialRef = Annotated[str | None, Field(max_length=64), AfterValidator(_credential_ref)]
HostList = Annotated[TextList, AfterValidator(_hosts)]
PathList = Annotated[TextList, AfterValidator(_paths)]


class SourceSegmentLink(Input):
    segment_id: uuid.UUID
    regions: TextList = []


class _SourceFields(Input):
    url: SourceUrl = None
    allowed_hosts: HostList = []
    allowed_paths: PathList = []
    max_pages: int = Field(default=3, ge=1, le=20)
    max_records: int = Field(default=5, ge=1, le=50)
    max_requests: int = Field(default=6, ge=1, le=50)
    timeout_seconds: int = Field(default=60, ge=5, le=120)
    refresh_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    credential_ref: CredentialRef = None
    store_raw: bool = False
    retention_days: int = Field(default=90, ge=1, le=3650)
    policy_notes: Text = Field(default="", max_length=2000)
    segments: list[SourceSegmentLink] = Field(default_factory=list, max_length=20)


class SourceInput(_SourceFields):
    name: Text = Field(min_length=1, max_length=200)
    kind: SourceKind

    @model_validator(mode="after")
    def _kind_rules(self) -> SourceInput:
        spec = SOURCE_KINDS[self.kind]
        if spec.needs_url and not self.url:
            raise ValueError(f"نوع المصدر «{spec.label}» يحتاج رابطًا")
        if self.kind == "linkedin" and self.url:
            host = check_url(self.url).host
            if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
                raise ValueError("رابط LinkedIn يجب أن يكون على linkedin.com")
        return self


class SourcePatch(Input):
    version: int
    name: Text | None = Field(default=None, min_length=1, max_length=200)
    url: SourceUrl = None
    allowed_hosts: HostList | None = None
    allowed_paths: PathList | None = None
    max_pages: int | None = Field(default=None, ge=1, le=20)
    max_records: int | None = Field(default=None, ge=1, le=50)
    max_requests: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    refresh_interval_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    credential_ref: CredentialRef = None
    store_raw: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    policy_notes: Text | None = Field(default=None, max_length=2000)
    segments: list[SourceSegmentLink] | None = Field(default=None, max_length=20)


class ActivateInput(Input):
    version: int
    policy_version: int
    confirmations: list[bool] = Field(
        min_length=len(SOURCE_POLICY_ITEMS), max_length=len(SOURCE_POLICY_ITEMS)
    )
    policy_notes: Text = Field(default="", max_length=2000)


def default_hosts(url: str | None) -> list[str]:
    if not url:
        return []
    host = check_url(url).host
    bare = host.removeprefix("www.")
    return [bare, f"www.{bare}"] if not check_url(url).is_ip_literal else [host]


def source_config(src: Source) -> SourceConfig:
    return SourceConfig(
        source_id=src.id,
        workspace_id=src.workspace_id,
        kind=src.kind,
        connector_key=src.connector_key,
        url=src.url,
        allowed_hosts=tuple(src.allowed_hosts),
        allowed_paths=tuple(src.allowed_paths),
        max_pages=src.max_pages,
        max_records=src.max_records,
        max_requests=src.max_requests,
        timeout_seconds=src.timeout_seconds,
        credential_ref=src.credential_ref,
        store_raw=src.store_raw,
        retention_days=src.retention_days,
        config_version=src.config_version,
        extra=dict(src.config),
    )


async def _set_segments(
    db: AsyncSession, p: Principal, source_id: uuid.UUID, links: list[SourceSegmentLink]
) -> None:
    ids = [link.segment_id for link in links]
    if len(set(ids)) != len(ids):
        raise InvalidInput("فئة مكررة في الربط")
    if ids:
        found = set(
            (
                await db.execute(
                    select(Segment.id).where(
                        Segment.workspace_id == p.workspace_id, Segment.id.in_(ids)
                    )
                )
            ).scalars()
        )
        if found != set(ids):
            raise InvalidInput("فئة غير موجودة", code="unknown_segment")
    await db.execute(delete(SourceSegment).where(SourceSegment.source_id == source_id))
    for link in links:
        db.add(
            SourceSegment(
                workspace_id=p.workspace_id,
                source_id=source_id,
                segment_id=link.segment_id,
                regions=link.regions,
            )
        )
    await db.flush()


def _check_url_in_hosts(src: Source) -> None:
    if not src.url or not SOURCE_KINDS[src.kind].fetches:
        return
    target = check_url(src.url)
    if not host_allowed(target.host, list(src.allowed_hosts)):
        raise InvalidInput(
            "نطاق الرابط يجب أن يكون ضمن النطاقات المسموحة", code="url_host_not_allowed"
        )
    if not path_allowed(target.path, list(src.allowed_paths)):
        raise InvalidInput(
            "مسار الرابط يجب أن يكون ضمن المسارات المسموحة", code="url_path_not_allowed"
        )


async def list_sources(db: AsyncSession, p: Principal) -> list[Source]:
    q = (
        select(Source)
        .where(Source.workspace_id == p.workspace_id)
        .order_by(Source.status, Source.name)
    )
    return list((await db.execute(q)).scalars())


async def create_source(
    db: AsyncSession, p: Principal, settings: Settings, data: SourceInput
) -> Source:
    require_owner(p)
    spec = SOURCE_KINDS[data.kind]
    hosts = data.allowed_hosts or (default_hosts(data.url) if spec.fetches else [])
    src = Source(
        workspace_id=p.workspace_id,
        name=data.name,
        url=data.url,
        kind=data.kind,
        connector_key=connector_key_for(data.kind, settings),
        access_mode=spec.access_mode,
        allowed_hosts=hosts,
        allowed_paths=data.allowed_paths,
        max_pages=data.max_pages,
        max_records=data.max_records,
        max_requests=data.max_requests,
        timeout_seconds=data.timeout_seconds,
        refresh_interval_hours=data.refresh_interval_hours,
        credential_ref=data.credential_ref,
        store_raw=data.store_raw,
        retention_days=data.retention_days,
        policy_notes=data.policy_notes,
        status="new",
        config_version=1,
        created_by=p.actor_id,
        updated_by=p.actor_id,
    )
    _check_url_in_hosts(src)
    db.add(src)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("يوجد مصدر بالاسم نفسه", code="duplicate_name") from exc
    await _set_segments(db, p, src.id, data.segments)
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="source.create",
        entity_type="source",
        entity_id=src.id,
        entity_version=1,
        change={"name": src.name, "kind": src.kind, "url": src.url},
    )
    return src


async def update_source(
    db: AsyncSession, p: Principal, source_id: uuid.UUID, data: SourcePatch
) -> Source:
    require_owner(p)
    src = await get_scoped(db, Source, p.workspace_id, source_id, lock=True)
    if src.config_version != data.version:
        raise VersionConflict()
    values = data.model_dump(exclude_unset=True, exclude={"version", "segments"})
    config_changed = any(k in _CONFIG_FIELDS and getattr(src, k) != v for k, v in values.items())
    for key, val in values.items():
        setattr(src, key, val)
    if SOURCE_KINDS[src.kind].needs_url and not src.url:
        raise InvalidInput("هذا النوع يحتاج رابطًا")
    if "url" in values and "allowed_hosts" not in values and SOURCE_KINDS[src.kind].fetches:
        src.allowed_hosts = default_hosts(src.url)
    _check_url_in_hosts(src)
    src.config_version += 1
    src.updated_by = p.actor_id
    if config_changed and src.status not in ("new", "paused"):
        src.status = "new"
        src.status_reason = "تغيّر إعداد الوصول أو التخزين؛ يلزم فحص عينة وتأكيد جديدان قبل التفعيل"
        src.policy_confirmed_at = None
        src.policy_confirmed_by = None
    elif config_changed and src.status == "paused":
        src.policy_confirmed_at = None
        src.policy_confirmed_by = None
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("يوجد مصدر بالاسم نفسه", code="duplicate_name") from exc
    if data.segments is not None:
        await _set_segments(db, p, src.id, data.segments)
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="source.update",
        entity_type="source",
        entity_id=src.id,
        entity_version=src.config_version,
        change={**values, "config_changed": config_changed},
    )
    return src


async def latest_check(db: AsyncSession, source: Source) -> SourceCheck | None:
    return (
        await db.execute(
            select(SourceCheck)
            .where(SourceCheck.source_id == source.id)
            .order_by(SourceCheck.checked_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_sample_job(db: AsyncSession, source: Source) -> Job | None:
    return (
        await db.execute(
            select(Job)
            .where(
                Job.workspace_id == source.workspace_id,
                Job.kind == SOURCE_SAMPLE_JOB,
                Job.status.in_(("queued", "running", "retry_scheduled")),
                Job.payload["source_id"].astext == str(source.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def request_test(db: AsyncSession, p: Principal, source_id: uuid.UUID) -> Job:
    """يجدول فحص عينة. لا يتصل بالشبكة داخل طلب الويب؛ العامل ينفذ ضمن الحدود."""
    require_owner(p)
    src = await get_scoped(db, Source, p.workspace_id, source_id, lock=True)
    existing = await active_sample_job(db, src)
    if existing is not None:
        return existing
    job = await queue.enqueue(
        db,
        kind=SOURCE_SAMPLE_JOB,
        workspace_id=p.workspace_id,
        payload={
            "source_id": str(src.id),
            "config_version": src.config_version,
            "requested_by": p.actor_id,
        },
        max_attempts=2,
    )
    if src.status != "active":
        src.status = "validating"
        src.status_reason = "فحص العينة في الطابور"
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="source.test_requested",
        entity_type="source",
        entity_id=src.id,
        entity_version=src.config_version,
        change={"job_id": str(job.id)},
    )
    return job


def summarize_sample(result: SampleResult) -> dict[str, object]:
    """ملخص يُحفظ للمراجعة: الحقول المستخرجة المحدودة فقط، دون HTML خام."""
    return {
        "code": result.code,
        "message": result.message,
        "next_step": result.next_step,
        "synthetic": result.synthetic,
        "pages_fetched": result.pages_fetched,
        "retention_note": result.retention_note,
        "records": [
            {
                "id": r.source_record_id[:500],
                "url": r.url,
                "fields": {k: v[:300] for k, v in r.fields.items()},
                "links": r.links[:5],
            }
            for r in result.records[:10]
        ],
    }


async def record_check(
    db: AsyncSession,
    src: Source,
    result: SampleResult,
    *,
    config_version: int,
    job_id: uuid.UUID | None,
    duration_ms: int,
) -> SourceCheck:
    now = datetime.now(UTC)
    check = SourceCheck(
        workspace_id=src.workspace_id,
        source_id=src.id,
        job_id=job_id,
        status=result.status,
        config_version=config_version,
        connector_key=src.connector_key,
        sample_summary=summarize_sample(result),
        allowed_fields=result.fields_found,
        missing_fields=result.fields_missing,
        errors=[i.model_dump() for i in result.issues],
        request_count=result.request_count,
        bytes_fetched=result.bytes_fetched,
        duration_ms=duration_ms,
        cost_amount=result.cost_amount,
        cost_currency=result.cost_currency,
        expires_at=now + timedelta(days=CHECK_RETENTION_DAYS),
    )
    db.add(check)
    # نتيجة فحص لإعداد قديم تُسجل للمرجع لكنها لا تغير حالة المصدر.
    if config_version == src.config_version:
        if result.status == "succeeded":
            src.last_success_at = now
            src.last_error = None
            if src.status != "active":
                src.status = "sample_ready"
                src.status_reason = "العينة جاهزة للمراجعة؛ التفعيل يحتاج تأكيد المالك"
        else:
            src.last_error = result.message
            src.status = result.status
            src.status_reason = result.message + (
                f" — {result.next_step}" if result.next_step else ""
            )
    await db.flush()
    audit.record(
        db,
        workspace_id=src.workspace_id,
        actor_id="service:worker",
        action="source.checked",
        entity_type="source",
        entity_id=src.id,
        entity_version=config_version,
        change={"status": result.status, "code": result.code, "requests": result.request_count},
    )
    return check


async def activate_source(
    db: AsyncSession, p: Principal, source_id: uuid.UUID, data: ActivateInput
) -> Source:
    require_owner(p)
    src = await get_scoped(db, Source, p.workspace_id, source_id, lock=True)
    if src.config_version != data.version:
        raise VersionConflict()
    if data.policy_version != SOURCE_POLICY_VERSION:
        raise Conflict(
            "تغيّرت قائمة سياسة المصادر؛ حدّث الصفحة وراجعها", code="policy_version_changed"
        )
    if not all(data.confirmations):
        raise InvalidInput(
            "يلزم تأكيد جميع بنود سياسة الاستخدام والتخزين", code="policy_not_confirmed"
        )
    if src.status not in ("sample_ready", "paused"):
        raise Conflict("التفعيل يتطلب عينة ناجحة لإعداد المصدر الحالي", code="sample_required")
    check = await latest_check(db, src)
    if check is None or check.status != "succeeded" or check.config_version != src.config_version:
        raise Conflict("لا توجد عينة ناجحة للإعداد الحالي؛ شغّل الفحص أولًا", code="sample_required")
    src.status = "active"
    src.status_reason = None
    src.policy_version = SOURCE_POLICY_VERSION
    src.policy_confirmed_by = p.actor_id
    src.policy_confirmed_at = datetime.now(UTC)
    if data.policy_notes:
        src.policy_notes = data.policy_notes
    await db.flush()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="source.activate",
        entity_type="source",
        entity_id=src.id,
        entity_version=src.config_version,
        change={"policy_version": SOURCE_POLICY_VERSION, "check_id": str(check.id)},
    )
    return src


async def pause_source(
    db: AsyncSession, p: Principal, source_id: uuid.UUID, version: int
) -> Source:
    require_owner(p)
    src = await get_scoped(db, Source, p.workspace_id, source_id, lock=True)
    if src.config_version != version:
        raise VersionConflict()
    if src.status == "paused":
        return src
    src.status = "paused"
    src.status_reason = "أوقفه المالك"
    await db.flush()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="source.pause",
        entity_type="source",
        entity_id=src.id,
        entity_version=src.config_version,
    )
    return src
