"""إضافة عضو بشري مرتبط بحساب Supabase Auth موجود (التسجيل العام معطل).

    uv run python scripts/add_member.py --workspace-name "اسم الشركة" --auth-user-id <uuid من Supabase> \
        --email owner@example.com --name "الاسم" --role owner

ينشئ الـworkspace إن لم يوجد بالاسم نفسه. لا يطلب كلمة مرور ولا يلمس Supabase؛ الحساب يُنشأ من لوحة
Supabase (Invite user) ثم يُربط هنا بمعرفه.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db.models import Membership, UserProfile, Workspace
from app.db.session import create_engine, loop_factory, make_sessionmaker
from app.services import audit


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with make_sessionmaker(engine)() as db, db.begin():
            ws = (
                await db.execute(select(Workspace).where(Workspace.name == args.workspace_name))
            ).scalar_one_or_none()
            if ws is None:
                ws = Workspace(
                    name=args.workspace_name,
                    timezone=settings.app_timezone,
                    operating_mode="demo",
                    default_phone_region=args.phone_region,
                )
                db.add(ws)
                await db.flush()
            uid = uuid.UUID(args.auth_user_id)
            await db.execute(
                insert(UserProfile)
                .values(auth_user_id=uid, email=args.email.lower(), display_name=args.name)
                .on_conflict_do_update(
                    index_elements=[UserProfile.auth_user_id],
                    set_={"email": args.email.lower(), "display_name": args.name},
                )
            )
            await db.execute(
                insert(Membership)
                .values(workspace_id=ws.id, auth_user_id=uid, role=args.role, status="active")
                .on_conflict_do_update(
                    index_elements=[Membership.workspace_id, Membership.auth_user_id],
                    set_={"role": args.role, "status": "active"},
                )
            )
            audit.record(
                db,
                workspace_id=ws.id,
                actor_id="system:add_member",
                action="membership.upsert",
                entity_type="membership",
                change={"auth_user_id": str(uid), "role": args.role},
            )
        print(f"workspace={ws.id} member={uid} role={args.role}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--auth-user-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["owner", "reviewer"], required=True)
    parser.add_argument(
        "--phone-region", default=None, help="رمز دولة لتطبيع الهواتف المحلية، مثل SA"
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(parser.parse_args()), loop_factory=loop_factory())
