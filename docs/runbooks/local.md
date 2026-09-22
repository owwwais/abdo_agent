# تشغيل محلي وحل المشكلات

## التشغيل

راجع README. الخلاصة: `localdb.py start` → `alembic upgrade head` → `seed_demo.py` → `python -m app.main` + `python -m app.jobs.worker`.

## مشكلات معروفة

| العرض | السبب | الحل |
|---|---|---|
| `initdb: could not start process` في Windows | مسار غير ASCII | السكربت ينسخ الثنائيات إلى `%LOCALAPPDATA%\sales-agent` تلقائيًا؛ أو اضبط `LOCAL_PG_DIR` لمسار ASCII |
| `Psycopg cannot use the 'ProactorEventLoop'` | حلقة Windows الافتراضية | شغّل عبر `python -m app.main` / `python -m app.jobs.worker` (تضبط SelectorEventLoop)، لا `uvicorn` مباشرة |
| المصدر عالق «قيد الفحص» | العامل لا يعمل | شغّل العامل؛ صفحة «اليوم» تنبه عند غياب النبض |
| فحص مصدر ويب يعطي «الجلب الشبكي معطل» | `APP_ENV=demo` | اضبط `APP_ENV=development` و`SOURCE_FETCH_ENABLED=true` |
| `alembic` يفشل بترميز | ملف ini بغير UTF-8 | `alembic.ini` ASCII فقط؛ لا تضف تعليقات عربية فيه |

## إيقاف القاعدة المحلية

```bash
uv run python scripts/localdb.py stop
```

## إعادة بناء قاعدة التطوير (بيانات محلية فقط)

```bash
uv run alembic downgrade base && uv run alembic upgrade head && uv run python scripts/seed_demo.py
```
