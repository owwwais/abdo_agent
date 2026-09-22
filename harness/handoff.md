# التسليم — آخر حالة

**التاريخ:** 2026-09-22 · **المرحلة:** M0–M6 مبنية ومختبرة محليًا بموصلات اصطناعية. المتبقي: إدخال مفاتيح حقيقية ثم النشر (F-018).

## آخر ما تحقق

18 خاصية من 19 = `verified` محليًا، وF-018 (النشر والنسخ الاحتياطي) `in_progress`: النسخ الاحتياطي والاسترجاع مختبران والأدلة مكتوبة، لكن لم تُبنَ صورة Docker ولم يُنشر شيء. الأدلة في `harness/features.json` و`docs/verification/m0-m1.md` و`docs/verification/m2-m6.md`.

## الأوامر ونتائجها

- `uv run pytest -q` → **180 passed** (وحدة + تكامل على PostgreSQL حقيقي + HTML + E2E بمتصفح).
- `uv run ruff check` و`ruff format --check` و`mypy app scripts` → نظيف.
- `alembic upgrade head` من قاعدة فارغة → 0001، 0002، 0003 بلا انحراف.
- `scripts/backup.py dump` ثم `verify` → استرجاع ناجح في قاعدة مؤقتة.
- `scripts/eval_models.py` → يعمل، وحكم على المصنف الاصطناعي بـ«غير مقبول» لسقوطه في حالتي حقن.

## الملفات الأساسية (إضافة إلى ما سبق في M0/M1)

- الدورة: `app/workflows/` (`discovery.py`، `opportunity.py`، `processing.py`، `checkpoint.py`، `common.py`)
- النماذج والبوابة: `app/agents/` (`gateway.py`، `providers.py`، `prompts.py`)
- الاعتماد والإرسال: `app/services/approvals.py`، `outbound.py`، `policy.py`
- الوارد والقنوات: `app/services/inbound.py`، `telegram_bot.py`، `channels.py`، `app/connectors/mail.py`، `search.py`، `telegram.py`
- التشغيل: `app/jobs/scheduler.py`، `app/services/digest.py`، `cleanup.py`، `runs.py`، `budget.py`
- الإعدادات والأسرار: `app/services/integrations.py`، `secrets.py`، `connection_tests.py`، `app/web/settings_pages.py`
- الواجهة: `app/web/sales_pages.py` + قوالب `approvals/opportunities/opportunity_detail/conversations/runs`
- الواجهة البرمجية: `app/api/sales_api.py`، `app/api/webhooks.py`
- الأدلة: `docs/runbooks/launch-guide.md` (الدليل الكامل للمالك)، `backup-restore.md`، `docs/decisions/0007`

## العوائق (كلها تحتاج قرار المالك أو حسابه، لا برمجة)

| العائق | يمنع |
|---|---|
| لا مشروع Supabase ولا مفاتيحه | الدخول الحقيقي والنشر |
| لا مفتاح نموذج ولا اعتماد سياسة المعالجة | التشغيل الفعلي وتقييم نموذج حقيقي |
| لا مفتاح Brave | اكتشاف حقيقي (البحث يبقى اصطناعيًا) |
| لا كلمة مرور صندوق Hostinger ولا سر الويب هوك | الإرسال والاستقبال الحقيقيان |
| لا بوت تيليجرام ولا مجموعة | الاعتماد من تيليجرام (اللوحة بديل كامل) |
| لا ميزانية نقدية محددة | التحويل إلى وضع live (شرط في لوحة الجاهزية) |
| لا حساب Render ولا Docker محليًا | بناء الصورة والنشر |

## ديون معروفة (صغيرة)

- ربط شركة موجودة لا يكمل الحقول الفارغة من المصدر الجديد.
- إدارة الأعضاء عبر سكربت لا واجهة.
- `SIGTERM` غير مدعوم في العامل على Windows (Linux يعمل).
- المصدر التجريبي في قاعدة قديمة يبقى بـ`connector_key = fake_search`؛ التثبيت الجديد يستخدم `web_search`.
- تقييم النماذج يغطي دور الاستخراج (تصنيف الردود) فقط؛ لا تقييم آلي لجودة المسودات.
- لا مقياس زمني لاستجابة IMAP تحت ضغط؛ حجم الصندوق الحقيقي غير مُختبر.

## الخطوة التالية الدقيقة

1. المالك يتبع `docs/runbooks/launch-guide.md` من القسم 3: Supabase ← المفاتيح ← صفحة الإعدادات.
2. `scripts/eval_models.py --yes` على النموذج المختار قبل اعتماده.
3. تجربة مغلقة: `OUTBOUND_ALLOWLIST` ببريد المالك ثم `OUTBOUND_ENABLED=true`، وإرسال حقيقي واحد للتحقق من SMTP ومجلد المرسل والرد.
4. النشر على Render (F-018) ثم ويب هوك Hostinger، ثم تحويل وضع التشغيل إلى live.
5. بعد أسبوع تشغيل: مراجعة الأخطاء الحقيقية في «التشغيل والسجلات» وضبط حد التأهيل والميزانية.
