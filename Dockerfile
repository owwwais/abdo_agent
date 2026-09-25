# صورة واحدة للويب والعامل والمهام المجدولة؛ يختلف أمر التشغيل فقط.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# خلف موازن Render: ترويسات الوكيل تحدد https الصحيح. لا يُستخدم عنوان العميل لأي صلاحية خارج التطوير.
# scripts/start-web.sh يطبق الترحيلات أولًا إن كان MIGRATE_ON_START=true (خدمة واحدة مجانية)، ثم يشغّل uvicorn.
CMD ["sh", "scripts/start-web.sh"]
