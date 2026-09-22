# النشر (مرجع مختصر)

> الدليل الكامل بالخطوات والمفاتيح: [launch-guide.md](launch-guide.md). هذه الصفحة للتفاصيل التقنية فقط.
> النشر التلقائي معطل، والإرسال الحقيقي يبقى مغلقًا حتى قرارك.

1. **Supabase**: مشروع في منطقة يوافق عليها المؤسسون (قرار مفتوح SPEC §17). فعّل مفاتيح توقيع JWT غير المتماثلة، وعطّل التسجيل العام. أنشئ دور قاعدة مخصصًا يملك مخطط `sales` (docs/decisions/0002). لا تضف `sales` إلى Exposed schemas.
2. **Render**: أنشئ Blueprint من `render.yaml` (ويب + عامل + cron احتياطي). املأ مجموعة `sales-agent-shared`: `APP_BASE_URL` (https)، `DATABASE_URL` (**session pooler** على المنفذ 5432؛ لا transaction pooler لأن الحفظ الدائم يحتاج جلسة وprepared statements)، `SUPABASE_URL`، `SUPABASE_PUBLISHABLE_KEY`، `SESSION_SECRET` و`DATA_HASH_KEY` و`SECRETS_ENCRYPTION_KEY` (**احفظ الأخيرين خارج Render**: الأول تغييره يكسر مطابقة منع التواصل، والثاني فقدانه يعني إعادة إدخال كل مفاتيح الإعدادات)، و`HOSTINGER_WEBHOOK_SECRET`. الميزانيات والمفاتيح الأخرى من صفحة الإعدادات.
3. الترحيلات تعمل عبر `preDeployCommand: alembic upgrade head` في خدمة الويب.
4. أضف الأعضاء (docs/runbooks/members.md).
5. تحقق: `/health/ready` يعيد 200، الدخول يعمل، والتطبيق يرفض الإقلاع إن نقص أي متغير مطلوب (الرسالة في سجل Render).
6. مراقب خارجي لـ`/health/live`، ومتابعة نبض العامل من تبويب «الأعضاء والنظام».
7. النسخ الاحتياطي: [backup-restore.md](backup-restore.md). خذ نسخة قبل كل نشر يحمل ترحيلًا.

الرجوع لإصدار سابق: أعد نشر commit سابق من Render دون `alembic downgrade` على بيانات حقيقية؛ الترحيلات يجب أن تبقى متوافقة للخلف خلال الإصدار الواحد.

**إيقاف طارئ:** «اليوم ← التحكم ← إيقاف كل المعالجة» يعمل فورًا بلا نشر. وللإغلاق التام للإرسال: `OUTBOUND_ENABLED=false` ثم إعادة نشر. إيقاف خدمة العامل يوقف كل شيء، وتبقى المهام في الطابور حتى تشغيله.
