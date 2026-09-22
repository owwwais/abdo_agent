# وكيل البحث والمبيعات

نظام داخلي للمؤسسين: إعداد المنتجات والفئات والمصادر، وإضافة الجهات يدويًا أو من CSV مع إزالة تكرار محافظة، وفحص المصادر بأمان. المواصفة الكاملة في [SPEC.md](SPEC.md)، وحالة البناء في [harness/handoff.md](harness/handoff.md).

> **الحالة: محلي بموصلات اختبار (M0 + M1).** لا تكاملات حية: البحث والنماذج والبريد بموصلات `fake`، وSupabase غير مربوط بمشروع حقيقي، والإرسال الخارجي معطل. الدورة الكاملة (اكتشاف → تأهيل → مسودة → اعتماد → إرسال → رد) لم تُبنَ بعد (M2–M6).

## المتطلبات

- [uv](https://docs.astral.sh/uv/) (يثبت Python 3.12 تلقائيًا).
- PostgreSQL 16: إما السكربت المحلي (بلا Docker) أو `docker compose up db`.

## التشغيل المحلي

```bash
cp .env.example .env            # ثم عدّل APP_ENV=development إن أردت تجربة جلب مواقع حقيقية
uv sync
uv run python scripts/localdb.py start
uv run alembic upgrade head
uv run python scripts/seed_demo.py
uv run python -m app.main        # الواجهة على http://127.0.0.1:8000
uv run python -m app.jobs.worker # في طرفية ثانية: ينفذ فحوص المصادر
```

أو كل ذلك معًا: `scripts/dev.sh` (Git Bash/Linux/macOS).

ادخل من `/login` بزر «مالك تجريبي» (fixture محلي يعمل فقط في البيئات المحلية ومن الجهاز نفسه). مسار التجربة:

1. **المصادر** → «مقابلات وإحالات» → فحص عينة → راجع ثم أكد بنود السياسة → تفعيل.
2. **المنتجات** → «[تجريبي] منظم المواعيد» → تفعيل (الوصف مكتمل).
3. **الاستيراد** → أدخل جهة أو ارفع CSV → راجع المعاينة وأخطاء الصفوف → اعتماد.
4. **الشركات** → راجع الجهات، ومن «تحتاج مراجعة» حالات التكرار المحتمل.

## الاختبارات والفحوص

```bash
uv run pytest -q                 # 122 اختبارًا: وحدة + تكامل على PostgreSQL حقيقي + مسار HTML كامل
scripts/check.sh                 # ruff + format + mypy + pytest
harness/run.sh quick             # فحص سريع بلا قاعدة
```

الاختبارات تستخدم قاعدة `sales_test` (أو `TEST_DATABASE_URL`) وتعيد بناءها من الترحيلات في كل تشغيل. لا تحتاج شبكة خارجية: الجلب يُختبر بشبكة اصطناعية (`tests/fakes.py`).

## البنية

```
app/api        JSON API، الأخطاء، التحقق من الجلسة وCSRF
app/auth       Supabase Auth (JWKS)، جلسات الخادم، fixture التطوير
app/connectors عقد الموصلات، جالب HTTP آمن (SSRF)، HTML/RSS، FakeSearch، اليدوي
app/services   المنتجات والفئات، المصادر، التطبيع وإزالة التكرار، الاستيراد، التدقيق
app/jobs       طابور PostgreSQL والعامل ومعالجات المهام
app/web        صفحات HTML العربية
app/db         النماذج والجلسة
migrations     ترحيلات Alembic مرحلية (0001 = M0، 0002 = M1)
harness        سجل الخصائص والتقدم والتسليم للوكيل البرمجي
docs           القرارات، أدلة التشغيل، تقارير التحقق
```

## الإعداد

كل المتغيرات موثقة في [.env.example](.env.example). خارج `demo/development/test` يرفض التطبيق الإقلاع دون: `SESSION_SECRET` و`DATA_HASH_KEY` (32+ حرفًا)، Supabase، https، والميزانيتين؛ ويرفض `DEV_AUTH_ENABLED`. بيئة `demo` تفرض موصلات `fake` وتمنع الإرسال.

النشر المقترح: [render.yaml](render.yaml) (النشر التلقائي معطل) و[docs/runbooks/deploy.md](docs/runbooks/deploy.md). لا تنشر قبل اجتياز بوابات M6 والتفويض الصريح.
