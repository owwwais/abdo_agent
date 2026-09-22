"""بيانات demo موسومة: workspace تجريبي، مستخدمان fixture، الفئات الخمس، منتج تجريبي كمسودة، ومصدران جديدان.

    uv run python scripts/seed_demo.py

آمن للتكرار (idempotent). يرفض العمل خارج البيئات المحلية. لا يفعّل أي مصدر: التفعيل قرار المالك
بعد فحص العينة.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers import DEV_USERS
from app.config import Settings, get_settings
from app.connectors.registry import connector_key_for
from app.db.models import (
    Membership,
    Product,
    ProductSegment,
    Segment,
    Source,
    UserProfile,
    Workspace,
)
from app.db.session import create_engine, loop_factory, make_sessionmaker

DEMO_WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")

SEGMENTS = [
    (
        "الشركات الصغيرة والمتوسطة",
        "منشآت صغيرة ومتوسطة تحتاج أدوات تشغيل أو تواصل",
        ["فريق صغير يدير العمليات يدويًا"],
        [],
    ),
    (
        "المتاجر",
        "متاجر فعلية أو إلكترونية",
        ["متجر نشط بمنتجات معروضة"],
        ["متجر مغلق أو بلا نشاط ظاهر"],
    ),
    (
        "مشغلو الإقامة قصيرة المدى",
        "مشغلو ومالكو وحدات إقامة قصيرة؛ أولوية تجريبية لمتعددي الوحدات",
        ["يدير أكثر من وحدة"],
        ["صاحب إعلان لا يثبت ملكيته أو تشغيله للعقار"],
    ),
    (
        "العيادات ومحلات الخدمات بالمواعيد",
        "منشآت تعمل بالمواعيد",
        ["تعتمد على المواعيد"],
        ["لا تُجمع بيانات مرضى", "غياب حجز ظاهر ليس إثباتًا لغياب نظام داخلي"],
    ),
    (
        "الأفراد مقدمو الاستشارات والخدمات",
        "مستشارون ومقدمو خدمات تقنية ومهنية",
        ["يقدم خدمته للعموم باسم مهني"],
        [],
    ),
]


async def seed(db: AsyncSession, settings: Settings) -> None:
    await db.execute(
        insert(Workspace)
        .values(
            id=DEMO_WORKSPACE_ID,
            name="شركة تجريبية (demo)",
            timezone=settings.app_timezone,
            operating_mode="demo",
            default_phone_region="SA",
            is_demo_data=True,
        )
        .on_conflict_do_nothing()
    )
    for user in DEV_USERS.values():
        uid = uuid.UUID(user["auth_user_id"])
        await db.execute(
            insert(UserProfile)
            .values(
                auth_user_id=uid,
                email=user["email"],
                display_name=user["display_name"],
                is_dev_fixture=True,
            )
            .on_conflict_do_nothing()
        )
        await db.execute(
            insert(Membership)
            .values(
                workspace_id=DEMO_WORKSPACE_ID, auth_user_id=uid, role=user["role"], status="active"
            )
            .on_conflict_do_nothing()
        )
    for name, desc, fit, excl in SEGMENTS:
        await db.execute(
            insert(Segment)
            .values(
                workspace_id=DEMO_WORKSPACE_ID,
                name=name,
                description=desc,
                fit_rules=fit,
                exclusion_rules=excl,
            )
            .on_conflict_do_nothing()
        )
    await db.flush()
    clinic = (
        await db.execute(
            select(Segment).where(
                Segment.workspace_id == DEMO_WORKSPACE_ID,
                Segment.name == "العيادات ومحلات الخدمات بالمواعيد",
            )
        )
    ).scalar_one()
    product_name = "[تجريبي] منظم المواعيد"
    existing = (
        await db.execute(
            select(Product).where(
                Product.workspace_id == DEMO_WORKSPACE_ID, Product.name == product_name
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        product = Product(
            workspace_id=DEMO_WORKSPACE_ID,
            name=product_name,
            is_demo_data=True,
            status="draft",
            summary="منتج اصطناعي للتجربة فقط؛ ليس وصفًا لمنتج حقيقي.",
            problem="منشآت المواعيد تفقد حجوزات بسبب الرد اليدوي المتأخر على الاستفسارات.",
            capabilities=["صفحة حجز عامة", "تذكير بالموعد عبر البريد"],
            unavailable_capabilities=["تكامل مع أنظمة السجلات الطبية", "دفع إلكتروني"],
            fit_signals=["الحجز عبر الهاتف فقط"],
            exclusions=["منشأة لديها نظام حجز ظاهر ومفعل"],
            price_status="needs_review",
            priority=3,
            created_by="system:seed",
            updated_by="system:seed",
        )
        db.add(product)
        await db.flush()
        db.add(
            ProductSegment(
                workspace_id=DEMO_WORKSPACE_ID,
                product_id=product.id,
                segment_id=clinic.id,
                regions=["الرياض"],
            )
        )
    for name, kind in (
        ("مقابلات وإحالات", "manual"),
        ("بحث ويب (FakeSearch اصطناعي)", "web_search"),
    ):
        await db.execute(
            insert(Source)
            .values(
                workspace_id=DEMO_WORKSPACE_ID,
                name=name,
                kind=kind,
                connector_key=connector_key_for(kind, settings),
                access_mode="manual_only" if kind == "manual" else "api_key",
                status="new",
                is_demo_data=True,
                created_by="system:seed",
                updated_by="system:seed",
            )
            .on_conflict_do_nothing()
        )


async def main() -> None:
    settings = get_settings()
    if not settings.app_env.is_local:
        sys.exit("seed_demo يعمل فقط في demo/development/test")
    engine = create_engine(settings)
    try:
        async with make_sessionmaker(engine)() as db, db.begin():
            await seed(db, settings)
    finally:
        await engine.dispose()
    print(
        "تمت تهيئة بيانات demo. سجّل الدخول محليًا كمالك تجريبي، ثم افحص مصدر «مقابلات وإحالات» وفعّله."
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(), loop_factory=loop_factory())
