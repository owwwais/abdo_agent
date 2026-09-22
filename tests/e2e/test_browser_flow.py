"""E2E متصفح (F-016): خادم حقيقي على قاعدة الاختبار + Edge/Chromium عبر Playwright.

دخول تطوير → الموافقات → توثيق الأهلية → اعتماد → اتجاه RTL وعدم وجود تمرير أفقي على شاشة هاتف،
وأن حقول الأسرار في الإعدادات فارغة دائمًا. يُتخطى إن لم يتوفر متصفح محلي.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.auth.providers import DEV_USERS
from app.db.session import create_engine, loop_factory, make_sessionmaker
from tests.conftest import Member, WorkspaceFixture, make_settings
from tests.sales_fixtures import seed_draft

ROOT = Path(__file__).resolve().parents[2]
sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _prepare() -> None:
    from scripts.seed_demo import DEMO_WORKSPACE_ID, seed

    settings = make_settings()
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            tables = (
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'sales' AND tablename <> 'alembic_version'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            await conn.execute(
                text("TRUNCATE " + ", ".join(f"sales.{t}" for t in tables) + " CASCADE")
            )
        sm = make_sessionmaker(engine)
        async with sm() as db, db.begin():
            await seed(db, settings)
        owner, reviewer = DEV_USERS["owner"], DEV_USERS["reviewer"]
        ws = WorkspaceFixture(
            DEMO_WORKSPACE_ID,
            Member(uuid.UUID(owner["auth_user_id"]), "owner", owner["email"]),
            Member(uuid.UUID(reviewer["auth_user_id"]), "reviewer", reviewer["email"]),
        )
        await seed_draft(sm, ws, allowed=False)
    finally:
        await engine.dispose()


@pytest.fixture
def server(migrated_db: str) -> Iterator[str]:
    asyncio.run(_prepare(), loop_factory=loop_factory())
    port = _free_port()
    settings = make_settings()
    env = {
        **os.environ,
        "APP_ENV": "development",
        "DATABASE_URL": settings.database_url,
        "DEV_AUTH_ENABLED": "true",
        "APP_BASE_URL": f"http://127.0.0.1:{port}",
        "PORT": str(port),
        "OUTBOUND_ENABLED": "false",
        "SOURCE_FETCH_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{base}/health/live", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(
                    (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")[-2000:]
                )
            time.sleep(0.25)
        else:
            raise RuntimeError("الخادم لم يقلع")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _launch(p: object) -> object:
    chromium = p.chromium  # type: ignore[attr-defined]
    for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return chromium.launch(headless=True, **kwargs)
        except Exception:  # noqa: S112 - نجرب المتصفح التالي المتاح
            continue
    pytest.skip("لا يوجد متصفح محلي لـPlaywright (Edge/Chrome/Chromium)")


def test_login_review_and_approve_in_browser(server: str) -> None:
    with sync_api.sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(locale="ar-SA")  # type: ignore[attr-defined]
        page.goto(f"{server}/login")
        assert page.locator("html").get_attribute("dir") == "rtl"
        page.get_by_role("button", name="مالك تجريبي").click()
        page.wait_for_url(f"{server}/")
        assert page.get_by_role("heading", name="اليوم", exact=True).is_visible()

        page.goto(f"{server}/approvals")
        card = page.locator("article.card").first
        assert "مجمع عيادات الواحة" in card.inner_text()
        approve = card.get_by_role("button", name="اعتماد وإرسال")
        assert approve.is_disabled()  # الأهلية غير موثقة

        card.locator("select[name=status]").first.select_option("allowed")
        card.locator("input[name=basis]").first.fill("بريد منشور للتواصل التجاري (اختبار)")
        card.get_by_role("button", name="حفظ الأهلية").click()
        assert page.get_by_text("حُفظت أهلية التواصل").is_visible()

        page.locator("article.card").first.get_by_role("button", name="اعتماد وإرسال").click()
        assert page.get_by_text("اعتُمدت الرسالة").is_visible()
        assert page.get_by_text("معتمدة (1)").is_visible()

        page.goto(f"{server}/settings?tab=models")
        for field in page.locator("input[type=password]").all():
            assert field.input_value() == ""

        page.set_viewport_size({"width": 375, "height": 800})
        for path in (
            "/",
            "/approvals",
            "/opportunities",
            "/conversations",
            "/runs",
            "/companies",
            "/products",
            "/sources",
            "/imports",
            *(
                f"/settings?tab={t}"
                for t in ("operations", "models", "mail", "telegram", "search", "members")
            ),
        ):
            page.goto(f"{server}{path}")
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 1, (path, overflow)
        browser.close()  # type: ignore[attr-defined]
