"""تحميل كتالوج منتجات وفئات من ملف JSON إلى مساحة عمل، كمسودات للمراجعة.

    uv run python scripts/load_catalog.py private/catalog.json                 # معاينة فقط
    uv run python scripts/load_catalog.py private/catalog.json --yes           # كتابة فعلية
    uv run python scripts/load_catalog.py private/catalog.json --workspace-name "اسم الشركة" --yes

- الهدف: DATABASE_URL في .env (أو متغير البيئة). السكربت يطبع اسم الخادم قبل أي كتابة.
- الفئة الموجودة بالاسم نفسه تُستعمل كما هي (لا تُعدَّل)، والمنتج الموجود بالاسم نفسه يُتخطى.
- المنتجات تُنشأ «مسودة» دائمًا: راجعها، ثم «اختبر فهم النموذج للمنتج»، ثم فعّلها من اللوحة.
- التحقق عبر خدمات المنتجات نفسها (الحقول والحدود والتدقيق)، باسم مالك مساحة العمل.

صيغة الملف:
{"segments": [{"name", "description", "fit_rules": [], "exclusion_rules": []}],
 "products": [{"name", "summary", "problem", "capabilities": [], "unavailable_capabilities": [],
               "fit_signals": [], "exclusions": [], "product_url", "demo_url", "price_status",
               "price_text", "priority", "segments": [{"name", "regions": []}]}]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import Principal
from app.config import get_settings
from app.db.models import Membership, Product, Segment, UserProfile, Workspace
from app.db.session import create_engine, loop_factory, make_sessionmaker
from app.services import products as product_service


async def _workspace(db: AsyncSession, name: str | None) -> Workspace:
    q = select(Workspace).where(Workspace.is_demo_data.is_(False))
    if name:
        q = q.where(Workspace.name == name)
    found = list((await db.execute(q)).scalars())
    if len(found) != 1:
        names = [w.name for w in (await db.execute(select(Workspace))).scalars()]
        raise SystemExit(
            f"حدد مساحة العمل بـ--workspace-name؛ الموجود: {names or 'لا شيء'} "
            "(أنشئها أولًا بـscripts/add_member.py)"
        )
    return found[0]


async def _owner(db: AsyncSession, ws: Workspace) -> Principal:
    row = (
        await db.execute(
            select(Membership, UserProfile)
            .join(UserProfile, UserProfile.auth_user_id == Membership.auth_user_id)
            .where(
                Membership.workspace_id == ws.id,
                Membership.role == "owner",
                Membership.status == "active",
            )
            .limit(1)
        )
    ).first()
    if row is None:
        raise SystemExit("لا مالك فعال في مساحة العمل؛ اربط حسابك بـscripts/add_member.py أولًا")
    member, profile = row
    return Principal(
        auth_user_id=member.auth_user_id,
        workspace_id=ws.id,
        role="owner",
        email=profile.email,
        display_name=profile.display_name,
        session_id=uuid.uuid4(),
        csrf_token="",
        auth_method="script",
    )


async def load(db: AsyncSession, catalog: dict[str, Any], workspace_name: str | None) -> list[str]:
    """يطبق الكتالوج داخل معاملة المستدعي ويعيد سطور التقرير."""
    ws = await _workspace(db, workspace_name)
    p = await _owner(db, ws)
    report = [f"مساحة العمل: {ws.name}"]
    seg_ids: dict[str, uuid.UUID] = {}
    for s in catalog.get("segments", []):
        existing = (
            await db.execute(
                select(Segment).where(Segment.workspace_id == ws.id, Segment.name == s["name"])
            )
        ).scalar_one_or_none()
        if existing is not None:
            seg_ids[s["name"]] = existing.id
            report.append(f"فئة موجودة (لم تُعدَّل): {s['name']}")
            continue
        seg = await product_service.create_segment(db, p, product_service.SegmentInput(**s))
        seg_ids[s["name"]] = seg.id
        report.append(f"فئة جديدة: {s['name']}")
    for item in catalog.get("products", []):
        name = item["name"]
        exists = (
            await db.execute(
                select(Product.id).where(Product.workspace_id == ws.id, Product.name == name)
            )
        ).first()
        if exists is not None:
            report.append(f"منتج موجود (تُخطي): {name}")
            continue
        links = []
        for link in item.get("segments", []):
            if link["name"] not in seg_ids:
                raise SystemExit(f"المنتج «{name}» يشير إلى فئة غير معرفة: {link['name']}")
            links.append({"segment_id": seg_ids[link["name"]], "regions": link.get("regions", [])})
        data = product_service.ProductInput(
            **{k: v for k, v in item.items() if k != "segments"}, segments=links
        )
        await product_service.create_product(db, p, data)
        report.append(
            f"منتج جديد (مسودة): {name} · {len(data.capabilities)} خاصية · أولوية {data.priority}"
        )
    return report


async def main(catalog: dict[str, Any], workspace_name: str | None, apply: bool) -> None:
    settings = get_settings()
    host = urlsplit(settings.database_url.replace("+psycopg", "")).hostname
    print(f"قاعدة البيانات الهدف: {host}")
    engine = create_engine(settings)
    try:
        async with make_sessionmaker(engine)() as db:
            async with db.begin():
                report = await load(db, catalog, workspace_name)
                if not apply:
                    await db.rollback()
        print("\n".join(report))
        print(
            "\nكُتب في القاعدة. راجع المنتجات في اللوحة ثم فعّلها."
            if apply
            else "\nمعاينة فقط: لم يُكتب شيء. أعد التشغيل مع --yes للكتابة."
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--workspace-name")
    parser.add_argument("--yes", action="store_true", help="كتابة فعلية (بدونه معاينة فقط)")
    args = parser.parse_args()
    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    asyncio.run(main(data, args.workspace_name, args.yes), loop_factory=loop_factory())
