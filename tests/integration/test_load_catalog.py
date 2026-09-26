"""سكربت تحميل الكتالوج: فئات ومنتجات كمسودات، إعادة التشغيل لا تكرر، والمعاينة لا تكتب."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Product, ProductSegment, Segment
from scripts.load_catalog import load
from tests.conftest import WorkspaceFixture

CATALOG: dict[str, Any] = {
    "segments": [
        {
            "name": "مقدمو الخدمات بالمواعيد",
            "description": "منشآت صغيرة بالحجز المسبق",
            "fit_rules": ["تعمل بالمواعيد"],
            "exclusion_rules": ["مستشفيات كبيرة"],
        }
    ],
    "products": [
        {
            "name": "منتج مواعيد اختباري",
            "summary": "صفحة حجز",
            "problem": "الحجز بالهاتف يضيع المواعيد",
            "capabilities": ["صفحة حجز خاصة", "تذكير بالبريد"],
            "unavailable_capabilities": ["الدفع الإلكتروني"],
            "fit_signals": ["الحجز عبر الهاتف فقط"],
            "exclusions": [],
            "product_url": "https://booking.example/",
            "price_status": "needs_review",
            "price_text": "",
            "priority": 5,
            "segments": [{"name": "مقدمو الخدمات بالمواعيد", "regions": ["الرياض", "جدة"]}],
        }
    ],
}


async def count(sm: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sm() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_loads_drafts_and_is_idempotent(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    async with sm() as db, db.begin():
        first = await load(db, CATALOG, None)
    assert "فئة جديدة: مقدمو الخدمات بالمواعيد" in first
    assert any(line.startswith("منتج جديد (مسودة): منتج مواعيد اختباري") for line in first)
    async with sm() as db:
        product = (await db.execute(select(Product))).scalar_one()
        link = (await db.execute(select(ProductSegment))).scalar_one()
    assert product.status == "draft" and product.priority == 5
    assert product.capabilities == ["صفحة حجز خاصة", "تذكير بالبريد"]
    assert link.regions == ["الرياض", "جدة"]
    async with sm() as db, db.begin():
        second = await load(db, CATALOG, None)
    assert "منتج موجود (تُخطي): منتج مواعيد اختباري" in second
    assert await count(sm, Product) == 1 and await count(sm, Segment) == 1


async def test_preview_rolls_back_and_bad_links_fail(
    sm: async_sessionmaker[AsyncSession], ws: WorkspaceFixture
) -> None:
    async with sm() as db:
        async with db.begin():
            await load(db, CATALOG, None)
            await db.rollback()
    assert await count(sm, Product) == 0  # المعاينة لا تكتب
    broken = {
        **CATALOG,
        "products": [{**CATALOG["products"][0], "segments": [{"name": "غير موجودة"}]}],
    }
    with pytest.raises(SystemExit, match="فئة غير معرفة"):
        async with sm() as db, db.begin():
            await load(db, broken, None)
