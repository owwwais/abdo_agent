# 0006 — طابور المهام وقاعدة التطوير المحلية

التاريخ: 2026-09-22 · الحالة: معتمد (الطابور أساسي في M1؛ التقوية في M5/F-013)

## الطابور

- جدول `sales.jobs` وعامل Python منفصل (`python -m app.jobs.worker`)، بلا Redis وبلا FastAPI BackgroundTasks.
- `claim`: `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …` ثم **commit فوري**؛ لا معاملة مفتوحة أثناء العمل الشبكي أو النموذج.
- **Fencing**: رقم المحاولة `attempts` يُحمل في الـLease. الاعتماد يتم في معاملة واحدة: `assert_lease` (قفل صف المهمة والتحقق من المالك والمحاولة وصلاحية الحجز) → كتابة النتائج → `complete`. عامل فقد حجزه يرفع `LeaseLost` ولا يترك أثرًا (T18).
- Heartbeat كل 30 ثانية يمدد الحجز (120 ثانية). `reap_expired` يعيد الحجوزات المنتهية للجدولة ضمن حد المحاولات.
- فشل مؤقت: backoff أسي (5s، 10s، … حتى 10 دقائق). فشل نهائي يستدعي hook خاصًا بالنوع (مثل إعادة المصدر من validating إلى failed).
- `idempotency_key` فريد لمنع إعادة جدولة الدورات (يستخدم في M5: `workspace:discover:local_date`).
- **متبقٍ لـM5**: قفل workspace لمسار المبيعات، حماية استئناف thread LangGraph، مهمة الجدولة اليومية، مهمة تنظيف الانتهاء، graceful shutdown على Windows (لا يدعم add_signal_handler؛ على Linux يعمل).

## PostgreSQL محلي دون Docker

- `scripts/localdb.py` يستخدم ثنائيات PostgreSQL 16 من حزمة `pgserver` (تطوير فقط)، على `127.0.0.1:54329`، بمصادقة trust محلية، وينشئ `sales` و`sales_test`.
- في Windows لا تعمل ثنائيات PostgreSQL من مسار يحوي أحرفًا غير ASCII (اسم المجلد العربي)، فتُنسخ الثنائيات والبيانات إلى `%LOCALAPPDATA%\sales-agent`.
- بديل Docker: `compose.yaml` (PostgreSQL 16 على المنفذ نفسه).
- الاختبارات تحذف مخطط `sales` في `sales_test` وتطبق كل الترحيلات من الصفر في كل تشغيل، وتفرغ الجداول قبل كل اختبار.
