"""مهمة Render cron: تستعيد الحجوزات المنتهية وتطبع حالة الطابور.

جدولة الاكتشاف اليومي والملخص المسائي بمفاتيح فريدة (workspace:discover:local_date) تُضاف في M5
(F-013). حتى ذلك الحين لا تنشئ هذه المهمة أي عمل جديد؛ لا تعوّض أيامًا فائتة.

    uv run python scripts/enqueue_due.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Job
from app.db.session import create_engine, loop_factory, make_sessionmaker
from app.jobs import queue


async def main() -> None:
    engine = create_engine(get_settings())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as db:
            reaped = await queue.reap_expired(db)
        from app.jobs.scheduler import schedule_due

        scheduled = await schedule_due(sm, get_settings())
        async with sm() as db:
            rows = (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all()
        counts = {status: n for status, n in rows}
        print(f"reaped={reaped} scheduled={len(scheduled)} jobs={counts}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=loop_factory())
