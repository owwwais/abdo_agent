"""يولد الأسرار الداخلية في ملف .env إن كانت فارغة: SESSION_SECRET وDATA_HASH_KEY وSECRETS_ENCRYPTION_KEY.

    uv run python scripts/gen_secrets.py            # يملأ الفارغ فقط ولا يطبع أي قيمة
    uv run python scripts/gen_secrets.py --print    # يطبع قيمًا جديدة لنسخها إلى لوحة الاستضافة

لا يستبدل قيمة موجودة أبدًا: تغيير DATA_HASH_KEY يكسر مطابقة سجل منع التواصل، وتغيير
SECRETS_ENCRYPTION_KEY دون إبقاء القديم يجعل الأسرار المحفوظة غير قابلة للقراءة.
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def fresh() -> dict[str, str]:
    return {
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "DATA_HASH_KEY": secrets.token_urlsafe(48),
        "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    values = fresh()
    if "--print" in sys.argv:
        for k, v in values.items():
            print(f"{k}={v}")
        return
    if not ENV.exists():
        sys.exit("لا يوجد .env؛ انسخ .env.example أولًا")
    text = ENV.read_text(encoding="utf-8")
    filled = []
    for key, value in values.items():
        pattern = re.compile(rf"^{key}=\s*$", re.M)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text, count=1)
            filled.append(key)
        elif not re.search(rf"^{key}=", text, re.M):
            text += f"\n{key}={value}\n"
            filled.append(key)
    ENV.write_text(text, encoding="utf-8")
    print("مُلئت: " + (", ".join(filled) if filled else "لا شيء (كلها مضبوطة مسبقًا)"))


if __name__ == "__main__":
    main()
