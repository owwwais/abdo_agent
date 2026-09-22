"""مسار كامل عبر نماذج HTML (دون متصفح): دخول محلي → فئة → منتج → تفعيل → مصدر → فحص → تفعيل → إدخال يدوي
→ CSV → اعتماد. يتحقق أن الصفحات تعرض الحالات والأخطاء بالعربية. اختبار المتصفح الفعلي (Playwright) في M6."""

from __future__ import annotations

import re

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import ClientFactory
from tests.integration.test_auth import _dev_login, _seed_dev_users
from tests.integration.test_sources import run_worker


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "لا يوجد رمز CSRF في الصفحة"
    return match.group(1)


def location_id(r: httpx.Response) -> str:
    return r.headers["location"].split("?")[0].rsplit("/", 1)[-1]


async def test_full_owner_flow_through_forms(
    client_for: ClientFactory, sm: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_dev_users(sm)
    c = await client_for(None)
    assert (await _dev_login(c)).status_code == 303
    today = await c.get("/")
    assert "جاهزية الإعداد" in today.text and "مطلوب قبل دورة البحث" in today.text
    token = csrf_of(today.text)

    # فئة بخطأ ثم تصحيح
    r = await c.post("/segments", data={"csrf_token": token, "name": ""})
    assert r.status_code == 422 and "راجع الحقول" in r.text
    r = await c.post(
        "/segments",
        data={"csrf_token": token, "name": "عيادات", "fit_rules": "تعمل بالمواعيد\nلديها موقع"},
    )
    assert r.status_code == 303
    segments_page = await c.get("/segments")
    seg_id = re.search(
        r'name="segment_ids" value="([^"]+)"', (await c.get("/products/new")).text
    ).group(1)  # type: ignore[union-attr]
    assert "عيادات" in segments_page.text

    # منتج: إنشاء ناقص، التفعيل محجوب، ثم إكمال وتفعيل
    r = await c.post(
        "/products", data={"csrf_token": token, "name": "منظم المواعيد", "priority": "4"}
    )
    assert r.status_code == 303
    pid = location_id(r)
    page = await c.get(f"/products/{pid}?ok=product_created")
    assert "أُنشئ المنتج كمسودة" in page.text and "وصف المشكلة" in page.text
    r = await c.post(
        f"/products/{pid}/status", data={"csrf_token": token, "status": "active", "version": "1"}
    )
    assert r.status_code == 422 and "لا يمكن تفعيل المنتج" in r.text
    r = await c.post(
        f"/products/{pid}",
        data={
            "csrf_token": token,
            "version": "1",
            "name": "منظم المواعيد",
            "problem": "ضياع الحجوزات",
            "capabilities": "صفحة حجز\nتذكير",
            "segment_ids": seg_id,
            "regions": "الرياض، جدة",
            "priority": "4",
            "price_status": "needs_review",
        },
    )
    assert r.status_code == 303
    r = await c.post(
        f"/products/{pid}/status", data={"csrf_token": token, "status": "active", "version": "2"}
    )
    assert r.status_code == 303
    page = await c.get(f"/products/{pid}")
    assert "نشط" in page.text and "الرياض, جدة" in page.text
    # تعديل بإصدار قديم → رسالة تعارض
    r = await c.post(f"/products/{pid}", data={"csrf_token": token, "version": "1", "name": "قديم"})
    assert r.status_code == 409 and "تغيّر السجل" in r.text

    # مصدر: رابط داخلي مرفوض، ثم مصدر يدوي وفحص وتفعيل
    r = await c.post(
        "/sources",
        data={"csrf_token": token, "name": "داخلي", "kind": "html", "url": "http://127.0.0.1/"},
    )
    assert r.status_code == 422 and "داخلي" in r.text
    r = await c.post(
        "/sources",
        data={
            "csrf_token": token,
            "name": "مقابلات وإحالات",
            "kind": "manual",
            "segment_ids": seg_id,
        },
    )
    assert r.status_code == 303
    sid = location_id(r)
    assert "لم يُفحص المصدر بعد" in (await c.get(f"/sources/{sid}")).text
    assert (await c.post(f"/sources/{sid}/test", data={"csrf_token": token})).status_code == 303
    pending = await c.get(f"/sources/{sid}")
    assert 'http-equiv="refresh"' in pending.text and "قيد الفحص" in pending.text
    await run_worker(sm)
    ready = await c.get(f"/sources/{sid}")
    assert "تأكيد وتفعيل المصدر" in ready.text
    r = await c.post(
        f"/sources/{sid}/activate",
        data={
            "csrf_token": token,
            "version": "1",
            "policy_version": "1",
            "confirm_0": "on",
            "confirm_1": "on",
        },
    )
    assert r.status_code == 422 and "جميع بنود" in r.text
    r = await c.post(
        f"/sources/{sid}/activate",
        data={
            "csrf_token": token,
            "version": "1",
            "policy_version": "1",
            "confirm_0": "on",
            "confirm_1": "on",
            "confirm_2": "on",
        },
    )
    assert r.status_code == 303

    # إدخال يدوي بخطأ ثم صحيح
    r = await c.post(
        "/imports/manual",
        data={"csrf_token": token, "source_id": sid, "name": "عيادة", "email": "x"},
    )
    assert r.status_code == 422 and "البريد الإلكتروني غير صالح" in r.text
    r = await c.post(
        "/imports/manual",
        data={
            "csrf_token": token,
            "source_id": sid,
            "name": "عيادة النور",
            "website": "alnoor.example",
            "phone": "0501234567",
            "segment": "عيادات",
        },
    )
    assert r.status_code == 303
    batch_page = await c.get(r.headers["location"])
    assert "سُجلت الجهة" in batch_page.text and "إنشاء شركة" in batch_page.text

    # CSV: معاينة ثم اعتماد
    csv_bytes = (
        "الاسم,الموقع\nعيادة النور,https://www.alnoor.example\nمتجر,\n,بلا اسم.example\n".encode()
    )
    r = await c.post(
        "/imports/csv",
        data={"csrf_token": token, "source_id": sid},
        files={"file": ("leads.csv", csv_bytes, "text/csv")},
    )
    assert r.status_code == 303
    preview = await c.get(r.headers["location"])
    assert (
        "المعاينة" in preview.text
        and "ربط بشركة موجودة" in preview.text
        and "الاسم مطلوب" in preview.text
    )
    bid = r.headers["location"].rsplit("/", 1)[-1]
    r = await c.post(f"/imports/{bid}/commit", data={"csrf_token": token})
    assert r.status_code == 303
    result = await c.get(f"/imports/{bid}")
    assert "معتمد" in result.text

    companies = await c.get("/companies")
    assert "عيادة النور" in companies.text and "متجر" in companies.text
    company_id = re.search(r'href="/companies/([0-9a-f-]{36})"', companies.text).group(1)  # type: ignore[union-attr]
    detail = await c.get(f"/companies/{company_id}")
    assert detail.status_code == 200 and "غير معروفة" in detail.text

    settings_page = await c.get("/settings")
    assert (
        "جاهزية التشغيل الفعلي" in settings_page.text
        and "حدد الميزانية اليومية" in settings_page.text
    )
    for tab in ("models", "mail", "telegram", "search", "members"):
        assert (await c.get(f"/settings?tab={tab}")).status_code == 200, tab
    today = await c.get("/")
    assert today.text.count("متوفر") == 3


async def test_reviewer_sees_read_only_forms(
    client_for: ClientFactory, sm: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_dev_users(sm)
    c = await client_for(None)
    assert (await _dev_login(c, "reviewer")).status_code == 303
    products = await c.get("/products")
    assert "منتج جديد" not in products.text
    sources = await c.get("/sources")
    assert "مصدر جديد" not in sources.text
    token = csrf_of((await c.get("/")).text)
    r = await c.post("/products", data={"csrf_token": token, "name": "محاولة"})
    assert r.status_code == 403 and "للمالك فقط" in r.text


async def test_html_404_for_unknown_ids(
    client_for: ClientFactory, sm: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_dev_users(sm)
    c = await client_for(None)
    await _dev_login(c)
    r = await c.get("/products/00000000-0000-4000-8000-000000000999")
    assert r.status_code == 404 and "غير موجود" in r.text
