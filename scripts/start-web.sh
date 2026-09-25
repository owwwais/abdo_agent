#!/bin/sh
# تشغيل خدمة الويب في الحاوية. MIGRATE_ON_START=true يطبق الترحيلات قبل الإقلاع
# (للخدمة الواحدة على خطة Render المجانية، حيث لا نعتمد على preDeployCommand).
# فشل الترحيل يوقف الإقلاع بدل تشغيل تطبيق على مخطط قديم.
set -e

if [ "${MIGRATE_ON_START:-false}" = "true" ]; then
  echo "تطبيق الترحيلات قبل الإقلاع..."
  alembic upgrade head
fi

exec uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*'
