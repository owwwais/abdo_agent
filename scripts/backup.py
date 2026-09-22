"""نسخ احتياطي واسترجاع لمخططي sales وsales_graph (الحالة الدائمة لـLangGraph).

    uv run python scripts/backup.py dump                     # إلى backups/sales-<التاريخ>.dump
    uv run python scripts/backup.py verify backups/X.dump    # يسترجع في قاعدة مؤقتة ويعد الصفوف ثم يحذفها
    uv run python scripts/backup.py restore backups/X.dump --target-url <URL> --yes

- يستخدم DATABASE_URL من .env مصدرًا. الملف بصيغة pg_dump المخصصة (-Fc) ويحوي بيانات حقيقية: خزنه مشفرًا
  وخارج المستودع (backups/ مستثنى من git).
- pg_dump يجب أن يكون إصداره الرئيسي ≥ إصدار الخادم. يُبحث عنه في PATH أولًا ثم في pgserver المحلي (16).
  لقاعدة Supabase (غالبًا 17) ثبّت أدوات عميل PostgreSQL المطابقة.
- الاسترجاع لا يعمل إلا على قاعدة هدف لا تحوي مخطط sales (لا دمج ولا كتابة فوق بيانات حقيقية).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ("sales", "sales_graph")


def _libpq_url(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    from scripts.localdb import _bin

    return _bin(name)


def _major(tool: str) -> int:
    out = subprocess.run([tool, "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+)(?:\.\d+)?", out)
    return int(m.group(1)) if m else 0


def _psql_value(url: str, sql: str) -> str:
    out = subprocess.run(
        [_tool("psql"), "-X", "-At", "-c", sql, f"--dbname={url}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _source_url() -> str:
    from app.config import get_settings

    url = get_settings().database_url
    if not url:
        sys.exit("DATABASE_URL غير مضبوط")
    return _libpq_url(url)


def _check_versions(tool: str, url: str) -> None:
    server = int(_psql_value(url, "show server_version_num")) // 10000
    client = _major(tool)
    if client < server:
        sys.exit(
            f"إصدار {Path(tool).name} ({client}) أقدم من الخادم ({server}). ثبّت أدوات عميل PostgreSQL {server}+"
        )


def _ascii_temp(name: str) -> Path:
    """أدوات PostgreSQL على Windows لا تقبل مسارات غير ASCII؛ نمر بمجلد مؤقت ASCII ثم ننقل الملف."""
    base = Path(tempfile.gettempdir())
    if not str(base).isascii():
        base = Path(os.environ.get("SystemDrive", "C:") + "\\") / "Temp"
    base.mkdir(parents=True, exist_ok=True)
    return base / name


def dump(out_dir: Path) -> Path:
    url = _source_url()
    tool = _tool("pg_dump")
    _check_versions(tool, url)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"sales-{datetime.now(UTC):%Y%m%d-%H%M%S}.dump"
    temp = _ascii_temp(name)
    cmd = [tool, "-Fc", "--no-owner", "--no-privileges", "-f", str(temp), f"--dbname={url}"]
    for s in SCHEMAS:
        cmd += ["-n", s]
    subprocess.run(cmd, check=True)
    target = out_dir / name
    shutil.move(str(temp), target)
    print(f"نسخة: {target} ({target.stat().st_size // 1024} ك.ب)")
    return target


def _restore(file: Path, url: str) -> None:
    exists = _psql_value(url, "select count(*) from pg_namespace where nspname = 'sales'")
    if exists != "0":
        sys.exit("القاعدة الهدف تحوي مخطط sales؛ الاسترجاع يحتاج قاعدة فارغة (لا كتابة فوق بيانات)")
    tool = _tool("pg_restore")
    temp = _ascii_temp(f"restore-{uuid.uuid4().hex[:8]}.dump")
    shutil.copyfile(file, temp)
    try:
        subprocess.run(
            [
                tool,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={url}",
                str(temp),
            ],
            check=True,
        )
    finally:
        temp.unlink(missing_ok=True)


def _with_db(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + db, parts.query, parts.fragment))


def verify(file: Path) -> None:
    """يسترجع النسخة في قاعدة مؤقتة على الخادم نفسه، يعد صفوف الجداول الأساسية، ثم يحذفها."""
    url = _source_url()
    temp = f"restore_check_{uuid.uuid4().hex[:8]}"
    _psql_value(url, f'create database "{temp}"')
    try:
        target = _with_db(url, temp)
        _restore(file, target)
        counts = {
            t: _psql_value(target, f"select count(*) from sales.{t}")  # noqa: S608 - أسماء جداول ثابتة
            for t in ("workspaces", "companies", "opportunities", "drafts", "messages", "audit_log")
        }
        print("استرجاع ناجح في قاعدة مؤقتة:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    finally:
        _psql_value(url, f'drop database if exists "{temp}"')


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", default=str(ROOT / "backups"))
    v = sub.add_parser("verify")
    v.add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--target-url", required=True)
    r.add_argument("--yes", action="store_true", help="تأكيد صريح للاسترجاع في القاعدة الهدف")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.cmd == "dump":
        dump(Path(args.out))
    elif args.cmd == "verify":
        verify(Path(args.file))
    else:
        if not args.yes:
            sys.exit("أضف --yes للتأكيد. الاسترجاع يتطلب قاعدة هدف فارغة.")
        _restore(Path(args.file), _libpq_url(args.target_url))
        print("اكتمل الاسترجاع. شغّل: uv run alembic current  للتأكد من إصدار المخطط.")


if __name__ == "__main__":
    main()
