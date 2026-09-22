"""واجهة JSON. كل تعديل مصادق ومصرح ومحمي من CSRF؛ 409 لتعارض الإصدار و422 لمدخل غير صالح."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import serialize as ser
from app.api.deps import current_principal, get_db, get_settings_dep
from app.api.errors import InvalidInput
from app.auth.sessions import Principal
from app.config import Settings
from app.db.models import (
    Company,
    CompanyIdentifier,
    CompanySourceLink,
    Contact,
    ImportBatch,
    Product,
    Source,
)
from app.services import imports as import_service
from app.services import products as product_service
from app.services import sources as source_service
from app.services.common import Input, get_scoped

router = APIRouter(prefix="/api")


@router.get("/me")
async def me(p: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {
        "user_id": str(p.auth_user_id),
        "email": p.email,
        "display_name": p.display_name,
        "role": p.role,
        "workspace_id": str(p.workspace_id),
        "csrf_token": p.csrf_token,
    }


# ---------------------------------------------------------------- الفئات


@router.get("/segments")
async def list_segments(
    p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return {
        "items": [
            ser.segment_out(s)
            for s in await product_service.list_segments(db, p, include_archived=True)
        ]
    }


@router.post("/segments", status_code=201)
async def create_segment(
    data: product_service.SegmentInput,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    seg = await product_service.create_segment(db, p, data)
    await db.commit()
    return ser.segment_out(seg)


@router.patch("/segments/{segment_id}")
async def update_segment(
    segment_id: uuid.UUID,
    data: product_service.SegmentPatch,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    seg = await product_service.update_segment(db, p, segment_id, data)
    await db.commit()
    return ser.segment_out(seg)


# ---------------------------------------------------------------- المنتجات


async def _product_detail(db: AsyncSession, p: Principal, product: Product) -> dict[str, Any]:
    links = await product_service.product_segments(db, p, product.id)
    problems = await product_service.activation_problems(db, product)
    out = ser.product_out(
        product,
        [{"segment_id": str(s.id), "name": s.name, "regions": ps.regions} for ps, s in links],
    )
    out["activation_problems"] = problems
    return out


@router.get("/products")
async def list_products(
    p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return {"items": [ser.product_out(x) for x in await product_service.list_products(db, p)]}


@router.post("/products", status_code=201)
async def create_product(
    data: product_service.ProductInput,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    product = await product_service.create_product(db, p, data)
    out = await _product_detail(db, p, product)
    await db.commit()
    return out


@router.get("/products/{product_id}")
async def get_product(
    product_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _product_detail(db, p, await get_scoped(db, Product, p.workspace_id, product_id))


@router.patch("/products/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    data: product_service.ProductPatch,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    product = await product_service.update_product(db, p, product_id, data)
    out = await _product_detail(db, p, product)
    await db.commit()
    return out


class StatusChange(Input):
    status: Literal["draft", "active", "paused", "archived"]
    version: int


@router.post("/products/{product_id}/status")
async def change_product_status(
    product_id: uuid.UUID,
    data: StatusChange,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    product = await product_service.change_product_status(
        db, p, product_id, data.status, data.version
    )
    out = await _product_detail(db, p, product)
    await db.commit()
    return out


# ---------------------------------------------------------------- المصادر


@router.get("/sources")
async def list_sources(
    p: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return {"items": [ser.source_out(s) for s in await source_service.list_sources(db, p)]}


@router.post("/sources", status_code=201)
async def create_source(
    data: source_service.SourceInput,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    src = await source_service.create_source(db, p, settings, data)
    await db.commit()
    return ser.source_out(src)


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await get_scoped(db, Source, p.workspace_id, source_id)
    out = ser.source_out(src, await source_service.latest_check(db, src))
    job = await source_service.active_sample_job(db, src)
    out["pending_job"] = ser.job_out(job) if job else None
    return out


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: uuid.UUID,
    data: source_service.SourcePatch,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await source_service.update_source(db, p, source_id, data)
    await db.commit()
    return ser.source_out(src)


@router.post("/sources/{source_id}/test", status_code=202)
async def test_source(
    source_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    job = await source_service.request_test(db, p, source_id)
    await db.commit()
    return {"job": ser.job_out(job)}


@router.post("/sources/{source_id}/activate")
async def activate_source(
    source_id: uuid.UUID,
    data: source_service.ActivateInput,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await source_service.activate_source(db, p, source_id, data)
    await db.commit()
    return ser.source_out(src)


class VersionOnly(Input):
    version: int


@router.post("/sources/{source_id}/pause")
async def pause_source(
    source_id: uuid.UUID,
    data: VersionOnly,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await source_service.pause_source(db, p, source_id, data.version)
    await db.commit()
    return ser.source_out(src)


# ---------------------------------------------------------------- الاستيراد والشركات


class ManualImport(import_service.ManualEntry):
    source_id: uuid.UUID


@router.post("/imports/manual", status_code=201)
async def import_manual(
    data: ManualImport,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    row = data.model_dump(exclude={"source_id"})
    batch = await import_service.create_preview(
        db, p, settings, source_id=data.source_id, rows=[row], kind="manual"
    )
    preview = await import_service.batch_rows(db, p, batch.id)
    if preview[0].action == "skip_error":
        raise InvalidInput(
            "بيانات الجهة غير صالحة",
            details={"fields": {e["field"]: e["message"] for e in preview[0].errors}},
        )
    batch = await import_service.commit_batch(db, p, settings, batch.id)
    out = ser.batch_out(batch, await import_service.batch_rows(db, p, batch.id))
    await db.commit()
    return out


@router.post("/imports/csv", status_code=201)
async def import_csv(
    source_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    raw = await file.read(import_service.MAX_FILE_BYTES + 1)
    if not raw:
        raise InvalidInput("الملف فارغ", code="empty_file")
    rows, ignored = import_service.read_rows(import_service.decode_csv(raw))
    batch = await import_service.create_preview(
        db,
        p,
        settings,
        source_id=source_id,
        rows=rows,
        kind="csv",
        filename=file.filename,
        ignored_columns=ignored,
    )
    out = ser.batch_out(batch, await import_service.batch_rows(db, p, batch.id))
    await db.commit()
    return out


@router.get("/imports/{batch_id}")
async def get_batch(
    batch_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    batch = await get_scoped(db, ImportBatch, p.workspace_id, batch_id)
    return ser.batch_out(batch, await import_service.batch_rows(db, p, batch_id))


@router.post("/imports/{batch_id}/commit")
async def commit_batch(
    batch_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    batch = await import_service.commit_batch(db, p, settings, batch_id)
    out = ser.batch_out(batch, await import_service.batch_rows(db, p, batch_id))
    await db.commit()
    return out


@router.post("/imports/{batch_id}/cancel")
async def cancel_batch(
    batch_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    batch = await import_service.cancel_batch(db, p, batch_id)
    await db.commit()
    return ser.batch_out(batch)


@router.get("/companies")
async def list_companies(
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    q = select(Company).where(Company.workspace_id == p.workspace_id)
    if status:
        q = q.where(Company.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(q.order_by(Company.created_at.desc()).limit(limit).offset(max(0, offset)))
    ).scalars()
    return {
        "items": [ser.company_out(c) for c in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/companies/{company_id}")
async def get_company(
    company_id: uuid.UUID,
    p: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    c = await get_scoped(db, Company, p.workspace_id, company_id)
    idents = (
        await db.execute(select(CompanyIdentifier).where(CompanyIdentifier.company_id == c.id))
    ).scalars()
    links = (
        await db.execute(select(CompanySourceLink).where(CompanySourceLink.company_id == c.id))
    ).scalars()
    contacts = (await db.execute(select(Contact).where(Contact.company_id == c.id))).scalars()
    out = ser.company_out(c)
    out["identifiers"] = [
        {"kind": i.kind, "value": i.normalized_value, "strength": i.strength} for i in idents
    ]
    out["sources"] = [
        {
            "source_id": str(link.source_id),
            "ref": link.source_record_ref,
            "url": link.url,
            "first_seen_at": link.first_seen_at.isoformat(),
        }
        for link in links
    ]
    out["contacts"] = [
        {
            "channel": ct.channel,
            "value": ser.mask(ct.channel, ct.value),
            "normalized": ct.normalized,
            "contact_eligibility": "unknown",
        }
        for ct in contacts
    ]
    return out
