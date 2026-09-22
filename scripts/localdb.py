"""PostgreSQL محلي للتطوير والاختبار دون Docker، عبر ثنائيات حزمة pgserver (PostgreSQL 16).

الاستخدام:
    uv run python scripts/localdb.py start     # تهيئة عند الحاجة ثم تشغيل وإنشاء قواعد sales و sales_test
    uv run python scripts/localdb.py stop
    uv run python scripts/localdb.py status
    uv run python scripts/localdb.py url [dbname]

يستمع على 127.0.0.1 فقط. البيانات في .local/pg (خارج Git). للإنتاج استخدم Supabase.
في Windows لا تعمل ثنائيات PostgreSQL من مسار يحوي أحرفًا غير ASCII (مثل اسم مجلد عربي)،
لذلك تُنسخ الثنائيات والبيانات إلى %LOCALAPPDATA%/sales-agent عند الحاجة.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_WIN_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "sales-agent"


def _is_ascii(path: Path) -> bool:
    return str(path).isascii()


def _default_data_dir() -> Path:
    local = ROOT / ".local" / "pg"
    if sys.platform == "win32" and not _is_ascii(local):
        return _WIN_HOME / "pg"
    return local


DATA_DIR = Path(os.environ.get("LOCAL_PG_DIR", _default_data_dir()))
PORT = int(os.environ.get("LOCAL_PG_PORT", "54329"))
HOST = "127.0.0.1"
DATABASES = ("sales", "sales_test")


def _bin(name: str) -> str:
    try:
        import pgserver
    except ImportError:
        sys.exit("pgserver غير مثبت. شغّل: uv sync  (أو استخدم compose.yaml مع Docker)")
    exe = name + (".exe" if sys.platform == "win32" else "")
    install = Path(pgserver.__file__).parent / "pginstall"
    if sys.platform == "win32" and not _is_ascii(install):
        mirror = _WIN_HOME / "pginstall"
        if not (mirror / "bin" / exe).exists():
            shutil.copytree(install, mirror, dirs_exist_ok=True)
        install = mirror
    path = install / "bin" / exe
    if not path.exists():
        sys.exit(f"لم يُعثر على {exe} داخل pgserver")
    return str(path)


def url(dbname: str = "sales") -> str:
    return f"postgresql+psycopg://postgres@{HOST}:{PORT}/{dbname}"


def _running() -> bool:
    if not (DATA_DIR / "PG_VERSION").exists():
        return False
    res = subprocess.run([_bin("pg_ctl"), "-D", str(DATA_DIR), "status"], capture_output=True)
    return res.returncode == 0


def start() -> None:
    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not (DATA_DIR / "PG_VERSION").exists():
        subprocess.run(
            [
                _bin("initdb"),
                "-D",
                str(DATA_DIR),
                "-U",
                "postgres",
                "-E",
                "UTF8",
                "--no-locale",
                "--auth=trust",
            ],
            check=True,
            capture_output=True,
        )
    if not _running():
        log = DATA_DIR.parent / "pg.log"
        subprocess.run(
            [
                _bin("pg_ctl"),
                "-D",
                str(DATA_DIR),
                "-l",
                str(log),
                "-w",
                "-o",
                f"-h {HOST} -p {PORT}",
                "start",
            ],
            check=True,
            # لا تلتقط المخرجات: postgres يرث المقابض فيبقى الأنبوب مفتوحًا وينتظر run للأبد.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    for db in DATABASES:
        exists = subprocess.run(
            [
                _bin("psql"),
                "-h",
                HOST,
                "-p",
                str(PORT),
                "-U",
                "postgres",
                "-tAc",
                f"SELECT 1 FROM pg_database WHERE datname='{db}'",  # noqa: S608  ثابت داخلي
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if exists != "1":
            subprocess.run(
                [_bin("createdb"), "-h", HOST, "-p", str(PORT), "-U", "postgres", db], check=True
            )
    print(url())


def stop() -> None:
    if _running():
        subprocess.run([_bin("pg_ctl"), "-D", str(DATA_DIR), "-m", "fast", "stop"], check=True)
    print("stopped")


def main(argv: list[str]) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        print("running" if _running() else "stopped", url())
    elif cmd == "url":
        print(url(argv[2] if len(argv) > 2 else "sales"))
    elif cmd == "bin":
        print(_bin(argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
