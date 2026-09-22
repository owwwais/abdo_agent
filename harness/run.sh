#!/usr/bin/env bash
# مستويات الفحص للوكيل البرمجي:
#   harness/run.sh quick   → lint + اختبارات الوحدة (ثوانٍ، بلا قاعدة)
#   harness/run.sh db      → + اختبارات التكامل على PostgreSQL
#   harness/run.sh full    → كل الفحوص (scripts/check.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
level="${1:-db}"
case "$level" in
  quick) uv run ruff check app scripts tests && uv run pytest -q tests/unit ;;
  db)    uv run python scripts/localdb.py start >/dev/null && uv run pytest -q tests/unit tests/integration ;;
  full)  scripts/check.sh ;;
  *) echo "usage: harness/run.sh [quick|db|full]"; exit 2 ;;
esac
