#!/usr/bin/env bash
# تشغيل محلي كامل: قاعدة محلية + ترحيلات + seed demo + الويب والعامل معًا. Ctrl+C يوقف الاثنين.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { cp .env.example .env; echo "أُنشئ .env من .env.example"; }
uv sync
uv run python scripts/localdb.py start
uv run alembic upgrade head
uv run python scripts/seed_demo.py
uv run python -m app.jobs.worker &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
uv run python -m app.main
