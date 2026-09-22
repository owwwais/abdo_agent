# النشر (مسودة — غير مفعّل)

> لا تنشر قبل اجتياز بوابات M6 وتفويض صريح. هذه خطوات إعداد، لا تفويض.

1. **Supabase**: مشروع في منطقة يوافق عليها المؤسسون (قرار مفتوح SPEC §17). فعّل مفاتيح توقيع JWT غير المتماثلة، وعطّل التسجيل العام. أنشئ دور قاعدة مخصصًا يملك مخطط `sales` (docs/decisions/0002). لا تضف `sales` إلى Exposed schemas.
2. **Render**: أنشئ Blueprint من `render.yaml`. املأ مجموعة `sales-agent-shared`: `APP_BASE_URL` (https)، `DATABASE_URL` (اتصال مباشر أو session pooler)، `SUPABASE_URL`، `SUPABASE_PUBLISHABLE_KEY`، `SESSION_SECRET` و`DATA_HASH_KEY` (قيم عشوائية 32+ حرفًا مختلفة؛ **احفظ DATA_HASH_KEY** فتغييره يكسر مطابقة سجل منع التواصل)، والميزانيتين.
3. الترحيلات تعمل عبر `preDeployCommand: alembic upgrade head` في خدمة الويب.
4. أضف الأعضاء (docs/runbooks/members.md).
5. تحقق: `/health/ready` يعيد 200، الدخول يعمل، والتطبيق يرفض الإقلاع إن نقص أي متغير مطلوب (الرسالة في سجل Render).
6. مراقب خارجي لـ`/health/live` ونبض العامل (M5/F-015).

الرجوع لإصدار سابق: أعد نشر commit سابق من Render دون `alembic downgrade` على بيانات حقيقية؛ الترحيلات يجب أن تبقى متوافقة للخلف خلال الإصدار الواحد (runbook كامل في M6).
