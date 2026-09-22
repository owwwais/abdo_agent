#!/usr/bin/env bash
# فحوص الجودة الكاملة دون شبكة خارجية: lint + format + أنواع + ترحيلات + اختبارات.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check app scripts tests
uv run ruff format --check app scripts tests migrations
uv run mypy app scripts
uv run python scripts/localdb.py start >/dev/null
uv run pytest -q "$@"
