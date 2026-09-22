"""F-005: استيراد يدوي وCSV مع معاينة وأخطاء صفوف، وإزالة تكرار محافظة (T06، T07)، ومنع التواصل."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import data_hash
from app.config import Settings
from app.db.models import (
    Company,
    CompanyDuplicateCandidate,
    CompanySourceLink,
    Contact,
    ImportRow,
    Suppression,
)
from tests.conftest import ClientFactory, WorkspaceFixture
from tests.integration.test_sources import CONFIRM, run_worker

CSV = (
    "الاسم,الموقع,الهاتف,البريد,الفئة,السجل التجاري,عمود غريب\n"
    "عيادة النور,https://alnoor-clinic.example,0501111111,info@alnoor-clinic.example,عيادات,,x\n"
    ",https://noname.example,,,,,\n"
    "متجر البن,https://salla.sa/bun-store,,not-an-email,,,\n"
    "متجر التمر,https://salla.sa/tamr-store,,,,123,\n"
    "مكرر النور,alnoor-clinic.example,,,,,\n"
    "داخلي,http://10.0.0.1/,,,,,\n"
    "فئة مجهولة,,,,غير موجودة,,\n"
)


async def active_source(
    owner: httpx.AsyncClient, sm: async_sessionmaker[AsyncSession], name: str = "إحالات"
) -> dict[str, Any]:
    src = (await owner.post("/api/sources", json={"name": name, "kind": "manual"})).json()
    await owner.post(f"/api/sources/{src['id']}/test")
    await run_worker(sm)
    r = await owner.post(f"/api/sources/{src['id']}/activate", json={"version": 1, **CONFIRM})
    assert r.status_code == 200, r.text
    return r.json()


async def upload(
    client: httpx.AsyncClient, source_id: str, content: str | bytes, name: str = "leads.csv"
) -> httpx.Response:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return await client.post(
        "/api/imports/csv", data={"source_id": source_id}, files={"file": (name, data, "text/csv")}
    )


async def test_import_requires_active_source(
    client_for: ClientFactory, ws: WorkspaceFixture
) -> None:
    owner = await client_for(ws.owner)
    src = (await owner.post("/api/sources", json={"name": "إحالات", "kind": "manual"})).json()
    r = await owner.post("/api/imports/manual", json={"source_id": src["id"], "name": "جهة"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_not_active"


async def test_csv_preview_reports_row_errors_without_dropping_file(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    await owner.post("/api/segments", json={"name": "عيادات"})
    src = await active_source(owner, sm)
    r = await upload(owner, src["id"], CSV)
    assert r.status_code == 201, r.text
    batch = r.json()
    assert batch["status"] == "preview" and batch["row_count"] == 7
    assert batch["summary"]["ignored_columns"] == ["عمود غريب"]
    rows = {row["row_number"]: row for row in batch["rows"]}
    assert rows[2]["action"] == "create"
    assert rows[3]["action"] == "skip_error" and rows[3]["errors"][0]["field"] == "name"
    assert rows[4]["action"] == "skip_error" and rows[4]["errors"][0]["field"] == "email"
    assert rows[5]["action"] == "skip_error" and rows[5]["errors"][0]["field"] == "cr_number"
    assert rows[6]["action"] == "skip_duplicate_in_file" and "2" in rows[6]["match"]["reason"]
    assert rows[7]["action"] == "skip_error" and rows[7]["errors"][0]["field"] == "website"
    assert rows[8]["action"] == "skip_error" and rows[8]["errors"][0]["field"] == "segment"
    async with sm() as db:
        assert (
            await db.execute(select(func.count()).select_from(Company))
        ).scalar_one() == 0  # المعاينة لا تنشئ شيئًا

    committed = (await owner.post(f"/api/imports/{batch['id']}/commit")).json()
    assert committed["status"] == "committed"
    assert committed["summary"]["committed_counts"]["create"] == 1
    again = await owner.post(f"/api/imports/{batch['id']}/commit")
    assert again.status_code == 200 and again.json()["committed_at"] == committed["committed_at"]
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Company))).scalar_one() == 1
        leftover = (
            await db.execute(
                select(func.count()).select_from(ImportRow).where(ImportRow.data.is_not(None))
            )
        ).scalar_one()
        contact = (await db.execute(select(Contact).where(Contact.channel == "phone"))).scalar_one()
    assert leftover == 0  # بيانات الصفوف الخام حُذفت بعد الاعتماد
    assert contact.value == "+966501111111" and contact.normalized


async def test_same_company_from_two_sources_is_one_entity(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    """T06: الشركة في مصدرين → كيان واحد عند تحقق المعرف القوي، مع رابطين للمصدرين."""
    owner = await client_for(ws.owner)
    a = await active_source(owner, sm, "إحالات")
    b = await active_source(owner, sm, "مقابلات")
    r1 = await owner.post(
        "/api/imports/manual",
        json={
            "source_id": a["id"],
            "name": "عيادة النور",
            "website": "https://www.alnoor.example/",
        },
    )
    r2 = await owner.post(
        "/api/imports/manual",
        json={"source_id": b["id"], "name": "مجمع النور الطبي", "website": "alnoor.example"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r2.json()["rows"][0]["action"] == "link"
    assert r1.json()["rows"][0]["company_id"] == r2.json()["rows"][0]["company_id"]
    async with sm() as db:
        links = (await db.execute(select(CompanySourceLink))).scalars().all()
    assert {str(link.source_id) for link in links} == {a["id"], b["id"]}


async def test_shared_phone_does_not_merge_branches(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    """T07: فرعان برقم مشترك → لا دمج تلقائي؛ الثاني يحتاج مراجعة مع مرشح تكرار."""
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    first = (
        await owner.post(
            "/api/imports/manual",
            json={"source_id": src["id"], "name": "صيدلية الشفاء - العليا", "phone": "0112345678"},
        )
    ).json()
    second = (
        await owner.post(
            "/api/imports/manual",
            json={
                "source_id": src["id"],
                "name": "صيدلية الشفاء - الملز",
                "phone": "+966 11 234 5678",
            },
        )
    ).json()
    assert second["rows"][0]["action"] == "review"
    c1, c2 = first["rows"][0]["company_id"], second["rows"][0]["company_id"]
    assert c1 != c2
    async with sm() as db:
        company2 = await db.get(Company, c2)
        cand = (await db.execute(select(CompanyDuplicateCandidate))).scalar_one()
    assert company2 is not None and company2.status == "needs_review"
    assert (
        str(cand.company_id) == c2
        and str(cand.candidate_company_id) == c1
        and "رقم هاتف مشترك" in cand.reasons
    )


async def test_name_only_match_is_review_not_merge(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    await owner.post("/api/imports/manual", json={"source_id": src["id"], "name": "شركة الأمل"})
    r = (
        await owner.post(
            "/api/imports/manual", json={"source_id": src["id"], "name": "مؤسسة الامل"}
        )
    ).json()
    assert r["rows"][0]["action"] == "review"
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Company))).scalar_one() == 2


async def test_multi_tenant_platform_stores_stay_separate(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    a = (
        await owner.post(
            "/api/imports/manual",
            json={"source_id": src["id"], "name": "متجر أ", "website": "https://salla.sa/store-a"},
        )
    ).json()
    b = (
        await owner.post(
            "/api/imports/manual",
            json={"source_id": src["id"], "name": "متجر ب", "website": "https://salla.sa/store-b"},
        )
    ).json()
    assert a["rows"][0]["action"] == "create" and b["rows"][0]["action"] == "create"


async def test_conflicting_strong_identifiers_are_not_applied(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    await owner.post(
        "/api/imports/manual", json={"source_id": src["id"], "name": "أ", "website": "a.example"}
    )
    await owner.post(
        "/api/imports/manual", json={"source_id": src["id"], "name": "ب", "cr_number": "1010000001"}
    )
    r = (
        await owner.post(
            "/api/imports/manual",
            json={
                "source_id": src["id"],
                "name": "خليط",
                "website": "a.example",
                "cr_number": "1010000001",
            },
        )
    ).json()
    assert r["rows"][0]["action"] == "skip_conflict" and r["rows"][0]["company_id"] is None


async def test_suppressed_contacts_are_not_reimported(
    client_for: ClientFactory,
    ws: WorkspaceFixture,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    async with sm() as db, db.begin():
        db.add(
            Suppression(
                workspace_id=ws.id,
                scope="domain",
                target_hash=data_hash(settings, "domain", "optout.example"),
                reason="طلب إيقاف التواصل",
                created_by="test",
            )
        )
    r = await upload(
        owner, src["id"], "name,website\nجهة أوقفت التواصل,https://www.optout.example/\n"
    )
    row = r.json()["rows"][0]
    assert row["action"] == "skip_suppressed"
    async with sm() as db:
        stored = (await db.execute(select(ImportRow))).scalar_one()
    assert stored.data is None  # لا تُحفظ بيانات جهة في سجل منع التواصل حتى مؤقتًا


async def test_invalid_manual_entry_returns_422(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    r = await owner.post(
        "/api/imports/manual", json={"source_id": src["id"], "name": "جهة", "email": "bad"}
    )
    assert r.status_code == 422 and "email" in r.json()["error"]["details"]["fields"]
    async with sm() as db:
        assert (await db.execute(select(func.count()).select_from(Company))).scalar_one() == 0


async def test_contacts_are_masked_and_eligibility_unknown(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    reviewer = await client_for(ws.reviewer)
    src = await active_source(owner, sm)
    r = (
        await reviewer.post(
            "/api/imports/manual",
            json={"source_id": src["id"], "name": "جهة", "email": "sales@firm.example"},
        )
    ).json()
    company = (await owner.get(f"/api/companies/{r['rows'][0]['company_id']}")).json()
    contact = company["contacts"][0]
    assert contact["value"] != "sales@firm.example" and contact["value"].endswith("@firm.example")
    assert contact["contact_eligibility"] == "unknown"


async def test_bad_files_rejected_cleanly(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    r = await upload(owner, src["id"], "website\nx.example\n")
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_name_column"
    r = await upload(owner, src["id"], b"")
    assert r.status_code == 422
    r = await upload(owner, src["id"], "name\n" + "x\n" * 2001)
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_rows"


async def test_cancel_clears_row_data(
    client_for: ClientFactory, ws: WorkspaceFixture, sm: async_sessionmaker[AsyncSession]
) -> None:
    owner = await client_for(ws.owner)
    src = await active_source(owner, sm)
    batch = (await upload(owner, src["id"], "name,phone\nجهة,0500000000\n")).json()
    assert (await owner.post(f"/api/imports/{batch['id']}/cancel")).json()["status"] == "canceled"
    assert (await owner.post(f"/api/imports/{batch['id']}/commit")).status_code == 409
    async with sm() as db:
        assert (await db.execute(select(ImportRow.data))).scalar_one() is None
