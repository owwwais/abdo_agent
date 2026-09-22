# التسليم — آخر حالة

**التاريخ:** 2026-09-22 · **المرحلة:** اكتمل M0 وM1 محليًا بموصلات اختبار. التالي M2.

## آخر ما تحقق

F-001، F-002، F-003، F-004، F-005 = `verified` محليًا (الأدلة في `harness/features.json` و`docs/verification/m0-m1.md`). F-013 منفذ جزئيًا (أساس الطابور مختبر).

## الأوامر ونتائجها

- `uv run pytest -q` → 122 passed.
- `uv run ruff check app scripts tests` و`uv run mypy app scripts` → نظيف.
- `alembic upgrade/check/downgrade/upgrade` → نجح بلا انحراف.

## الملفات الأساسية

- الإعداد والحواجز: `app/config.py`
- النماذج والترحيلات: `app/db/models.py`، `migrations/versions/0001_*.py` (M0)، `0002_*.py` (M1)
- الهوية: `app/auth/`، `app/api/deps.py`
- المصادر والجلب الآمن: `app/connectors/`، `app/services/sources.py`
- الاستيراد والتكرار: `app/services/imports.py`، `companies.py`، `normalize.py`
- الطابور والعامل: `app/jobs/`
- الواجهة: `app/web/pages.py`، `app/templates/`، `app/static/app.css`

## العوائق الحقيقية (لا تمنع البناء المحلي)

| العائق | يمنع |
|---|---|
| لا مشروع Supabase | التحقق الحي من الدخول (F-002 live) |
| لم يُختر مزود بحث ولا حسابه | موصل البحث الحقيقي (F-006 live) |
| لم يُختر مزود نماذج ولا مفاتيحه وسياسة المعالجة | F-007 live، F-017 |
| لم يُختر مزود بريد (Gmail/Microsoft/غيره) | F-010/F-011 live |
| لا بوت تيليجرام ولا مجموعة اختبار | F-009 live |
| لا ميزانية نقدية محددة | تشغيل live (مطلوبة في الإعداد خارج المحلي) |
| لا بيانات منتجات حقيقية ولا رابط منصة «فرصة» | استبدال بيانات demo |
| Docker غير مثبت محليًا | بناء الصورة والتحقق منها |

## قرارات جديدة

`docs/decisions/0001`–`0006`. أبرزها: Python 3.12؛ مخطط `sales` مع RLS رفض افتراضي والعزل في الكود والمفاتيح المركبة؛ `SUPABASE_PUBLISHABLE_KEY` بدل `SUPABASE_AUTH_PUBLIC_KEY` وإضافة `DATA_HASH_KEY`؛ الاستيراد يحتاج مصدرًا نشطًا؛ فصل «اختبار فهم المنتج» إلى F-019.

## ديون معروفة (صغيرة)

- ربط شركة موجودة لا يكمل الحقول الفارغة (قطاع/مدينة) من المصدر الجديد.
- لا مهمة تنظيف لانتهاء `source_checks` و`import_batches` بعد (M5).
- إدارة الأعضاء عبر سكربت لا واجهة.
- إشارة SIGTERM في Windows غير مدعومة في العامل (Linux يعمل).

## الخطوة التالية الدقيقة

1. ابدأ F-013 (المجدول): مهمة `discover_daily` بمفتاح `workspace:discover:<التاريخ المحلي>`، نافذة سماح، قفل workspace لمسار المبيعات، وتنظيف الانتهاء؛ لأنها اعتمادية F-006.
2. ثم F-006: `discover()` لموصلي `html` و`fake_search`، اختيار منتج/فئة/مصدر مرجح بلا تجويع، `skipped_configuration`، حدود 3 استعلامات و20 مرشحًا، وتسجيل المرشحين عبر `companies.apply_draft`.
3. ثم F-007: تثبيت LangGraph بعد التحقق من الإصدار والـPostgres checkpointer الرسمي وتسجيله في ADR 0001، ModelGateway مع FakeModel، جداول evidence/opportunities/drafts (ترحيل 0003).
