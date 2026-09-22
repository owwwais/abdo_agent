"""المنتجات والفئات المستهدفة: إنشاء وتعديل بتحقق تفاؤلي من الإصدار، وتفعيل مشروط."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Conflict, InvalidInput, VersionConflict
from app.auth.sessions import Principal
from app.db.models import Product, ProductSegment, Segment
from app.services import audit
from app.services.common import Input, PublicUrl, Text, TextList, get_scoped, require_owner

ProductStatus = Literal["draft", "active", "paused", "archived"]

_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "archived"},
    "active": {"paused", "archived"},
    "paused": {"active", "archived", "draft"},
    "archived": {"draft"},
}


# ---------------------------------------------------------------- الفئات


class SegmentInput(Input):
    name: Text = Field(min_length=1, max_length=200)
    description: Text = Field(default="", max_length=2000)
    fit_rules: TextList = []
    exclusion_rules: TextList = []


class SegmentPatch(Input):
    version: int
    name: Text | None = Field(default=None, min_length=1, max_length=200)
    description: Text | None = Field(default=None, max_length=2000)
    fit_rules: TextList | None = None
    exclusion_rules: TextList | None = None
    status: Literal["active", "archived"] | None = None


async def list_segments(
    db: AsyncSession, p: Principal, *, include_archived: bool = False
) -> list[Segment]:
    q = select(Segment).where(Segment.workspace_id == p.workspace_id)
    if not include_archived:
        q = q.where(Segment.status == "active")
    return list((await db.execute(q.order_by(Segment.name))).scalars())


async def create_segment(db: AsyncSession, p: Principal, data: SegmentInput) -> Segment:
    require_owner(p)
    seg = Segment(workspace_id=p.workspace_id, **data.model_dump())
    db.add(seg)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("يوجد فئة بالاسم نفسه", code="duplicate_name") from exc
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="segment.create",
        entity_type="segment",
        entity_id=seg.id,
        entity_version=1,
        change={"name": seg.name},
    )
    return seg


async def update_segment(
    db: AsyncSession, p: Principal, segment_id: uuid.UUID, data: SegmentPatch
) -> Segment:
    require_owner(p)
    await get_scoped(db, Segment, p.workspace_id, segment_id)
    values = data.model_dump(exclude_unset=True, exclude={"version"})
    try:
        res = await db.execute(
            update(Segment)
            .where(
                Segment.id == segment_id,
                Segment.workspace_id == p.workspace_id,
                Segment.version == data.version,
            )
            .values(**values, version=Segment.version + 1)
            .returning(Segment)
        )
    except IntegrityError as exc:
        raise Conflict("يوجد فئة بالاسم نفسه", code="duplicate_name") from exc
    seg = res.scalar_one_or_none()
    if seg is None:
        raise VersionConflict()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="segment.update",
        entity_type="segment",
        entity_id=seg.id,
        entity_version=seg.version,
        change=values,
    )
    return seg


# ---------------------------------------------------------------- المنتجات


class SegmentLink(Input):
    segment_id: uuid.UUID
    regions: TextList = []


class ProductFields(Input):
    summary: Text = Field(default="", max_length=1000)
    problem: Text = Field(default="", max_length=3000)
    capabilities: TextList = []
    unavailable_capabilities: TextList = []
    fit_signals: TextList = []
    exclusions: TextList = []
    product_url: PublicUrl = None
    demo_url: PublicUrl = None
    price_status: Literal["needs_review", "approved"] = "needs_review"
    price_text: Text = Field(default="", max_length=1000)
    priority: int = Field(default=3, ge=1, le=5)
    segments: list[SegmentLink] = Field(default_factory=list, max_length=20)


class ProductInput(ProductFields):
    name: Text = Field(min_length=1, max_length=200)


class ProductPatch(Input):
    version: int
    name: Text | None = Field(default=None, min_length=1, max_length=200)
    summary: Text | None = Field(default=None, max_length=1000)
    problem: Text | None = Field(default=None, max_length=3000)
    capabilities: TextList | None = None
    unavailable_capabilities: TextList | None = None
    fit_signals: TextList | None = None
    exclusions: TextList | None = None
    product_url: PublicUrl = None
    demo_url: PublicUrl = None
    price_status: Literal["needs_review", "approved"] | None = None
    price_text: Text | None = Field(default=None, max_length=1000)
    priority: int | None = Field(default=None, ge=1, le=5)
    segments: list[SegmentLink] | None = Field(default=None, max_length=20)


async def _segment_links(db: AsyncSession, product: Product) -> list[ProductSegment]:
    return list(
        (
            await db.execute(select(ProductSegment).where(ProductSegment.product_id == product.id))
        ).scalars()
    )


async def activation_problems(db: AsyncSession, product: Product) -> list[str]:
    """شروط التفعيل: وصف مشكلة، خصائص معتمدة، فئة نشطة واحدة على الأقل، وسعر واضح إن اعتُمد."""
    problems: list[str] = []
    if not product.problem.strip():
        problems.append("وصف المشكلة التي يعالجها المنتج مطلوب")
    if not product.capabilities:
        problems.append("أضف خاصية متاحة واحدة على الأقل")
    active_segments = (
        await db.execute(
            select(func.count())
            .select_from(ProductSegment)
            .join(
                Segment,
                (Segment.id == ProductSegment.segment_id)
                & (Segment.workspace_id == ProductSegment.workspace_id),
            )
            .where(ProductSegment.product_id == product.id, Segment.status == "active")
        )
    ).scalar_one()
    if not active_segments:
        problems.append("اربط المنتج بفئة مستهدفة نشطة واحدة على الأقل")
    if product.price_status == "approved" and not product.price_text.strip():
        problems.append("السعر المعتمد يحتاج نصًا واضحًا، أو اجعله «يحتاج مراجعة»")
    return problems


async def _set_segments(
    db: AsyncSession, p: Principal, product_id: uuid.UUID, links: list[SegmentLink]
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
    await db.execute(delete(ProductSegment).where(ProductSegment.product_id == product_id))
    for link in links:
        db.add(
            ProductSegment(
                workspace_id=p.workspace_id,
                product_id=product_id,
                segment_id=link.segment_id,
                regions=link.regions,
            )
        )
    await db.flush()


async def list_products(db: AsyncSession, p: Principal) -> list[Product]:
    q = (
        select(Product)
        .where(Product.workspace_id == p.workspace_id)
        .order_by(Product.status, Product.priority.desc(), Product.name)
    )
    return list((await db.execute(q)).scalars())


async def product_segments(
    db: AsyncSession, p: Principal, product_id: uuid.UUID
) -> list[tuple[ProductSegment, Segment]]:
    rows = await db.execute(
        select(ProductSegment, Segment)
        .join(
            Segment,
            (Segment.id == ProductSegment.segment_id)
            & (Segment.workspace_id == ProductSegment.workspace_id),
        )
        .where(
            ProductSegment.product_id == product_id, ProductSegment.workspace_id == p.workspace_id
        )
        .order_by(Segment.name)
    )
    return [(ps, s) for ps, s in rows.tuples()]


async def create_product(db: AsyncSession, p: Principal, data: ProductInput) -> Product:
    require_owner(p)
    values = data.model_dump(exclude={"segments"})
    product = Product(
        workspace_id=p.workspace_id,
        status="draft",
        version=1,
        created_by=p.actor_id,
        updated_by=p.actor_id,
        **values,
    )
    db.add(product)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("يوجد منتج بالاسم نفسه", code="duplicate_name") from exc
    await _set_segments(db, p, product.id, data.segments)
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="product.create",
        entity_type="product",
        entity_id=product.id,
        entity_version=1,
        change={"name": product.name},
    )
    return product


async def update_product(
    db: AsyncSession, p: Principal, product_id: uuid.UUID, data: ProductPatch
) -> Product:
    require_owner(p)
    current = await get_scoped(db, Product, p.workspace_id, product_id, lock=True)
    if current.version != data.version:
        raise VersionConflict()
    values = data.model_dump(exclude_unset=True, exclude={"version", "segments"})
    for key, val in values.items():
        setattr(current, key, val)
    current.version += 1
    current.updated_by = p.actor_id
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("يوجد منتج بالاسم نفسه", code="duplicate_name") from exc
    if data.segments is not None:
        await _set_segments(db, p, current.id, data.segments)
    if current.status == "active":
        problems = await activation_problems(db, current)
        if problems:
            # لا يبقى منتج نشطًا بوصف ناقص؛ الإلغاء عبر rollback من المستدعي.
            raise InvalidInput(
                "لا يمكن حفظ منتج نشط بهذه البيانات: " + "؛ ".join(problems),
                code="active_product_invalid",
            )
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action="product.update",
        entity_type="product",
        entity_id=current.id,
        entity_version=current.version,
        change={
            **values,
            **(
                {"segments": [str(s.segment_id) for s in data.segments]}
                if data.segments is not None
                else {}
            ),
        },
    )
    return current


async def change_product_status(
    db: AsyncSession, p: Principal, product_id: uuid.UUID, target: ProductStatus, version: int
) -> Product:
    require_owner(p)
    product = await get_scoped(db, Product, p.workspace_id, product_id, lock=True)
    if product.version != version:
        raise VersionConflict()
    if target == product.status:
        return product
    if target not in _TRANSITIONS[product.status]:
        raise Conflict(
            f"لا يمكن الانتقال من {product.status} إلى {target}", code="invalid_transition"
        )
    if target == "active":
        problems = await activation_problems(db, product)
        if problems:
            raise InvalidInput(
                "لا يمكن تفعيل المنتج: " + "؛ ".join(problems),
                code="activation_blocked",
                details={"problems": problems},
            )
    old = product.status
    product.status = target
    product.version += 1
    product.updated_by = p.actor_id
    await db.flush()
    audit.record(
        db,
        workspace_id=p.workspace_id,
        actor_id=p.actor_id,
        action=f"product.status.{target}",
        entity_type="product",
        entity_id=product.id,
        entity_version=product.version,
        change={"from": old, "to": target},
    )
    return product


async def product_segment_names(db: AsyncSession, product_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(Segment.name)
        .join(ProductSegment, ProductSegment.segment_id == Segment.id)
        .where(ProductSegment.product_id == product_id)
    )
    return list(rows.scalars())
