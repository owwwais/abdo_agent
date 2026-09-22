# 0003 — الهوية والجلسات وCSRF

التاريخ: 2026-09-22 · الحالة: معتمد · F-002

## Supabase Auth

- الدخول بالبريد وكلمة المرور من الخادم: `POST {SUPABASE_URL}/auth/v1/token?grant_type=password` مع ترويسة `apikey` = المفتاح المنشور (`sb_publishable_…`).
- التحقق من access token محليًا عبر JWKS العام `{SUPABASE_URL}/auth/v1/.well-known/jwks.json` (تخزين ≤ 10 دقائق حسب توصية Supabase، وإعادة الجلب عند `kid` مجهول). نقبل **ES256/RS256 فقط**؛ رموز HS256 (السر المشترك القديم) مرفوضة → يلزم تفعيل مفاتيح التوقيع غير المتماثلة في المشروع.
- الفحوص: التوقيع، `iss = {SUPABASE_URL}/auth/v1`، `aud = authenticated` (قابل للضبط)، `exp`، `sub`.
- بعد التحقق لا نحفظ رموز Supabase؛ ننشئ **جلسة خادم** مرتبطة بعضوية فعالة. تعطيل العضوية يبطل الجلسة فورًا (فحص مع كل طلب).
- التسجيل العام معطل في التطبيق (لا مسار تسجيل)، ويجب تعطيله أيضًا في لوحة Supabase. الأعضاء يُدعون من Supabase ثم يُربطون بـ`scripts/add_member.py`.
- **الحالة:** منطق التحقق مختبر بمفتاح ES256 محلي وJWKS محاكى (`tests/unit/test_supabase_auth.py`). **لم يُتحقق منه مع مشروع Supabase حقيقي** (live-blocked).

## الجلسات

- كعكة `sa_session` تحمل رمزًا عشوائيًا 256-bit؛ القاعدة تحفظ `HMAC(SESSION_SECRET, token)` فقط. `HttpOnly`، `SameSite=Lax`، و`Secure` عند https. خمول 12 ساعة، حد مطلق 7 أيام، إبطال عند الخروج.

## CSRF

- كل تعديل يعتمد على الكعكة يتطلب رمز CSRF المرتبط بالجلسة (حقل `csrf_token` في النماذج أو ترويسة `X-CSRF-Token` لـJSON)، مع رفض `Origin` مختلف عن التطبيق.
- نموذج الدخول يستخدم double-submit (`sa_login_csrf`، `SameSite=Strict`).
- CSP صارمة (`default-src 'self'`، بلا inline)، و`X-Frame-Options: DENY`.

## fixture الدخول المحلي

- `DEV_AUTH_ENABLED=true` يعمل فقط إذا اجتمعت: بيئة محلية (demo/development/test) + تفعيل صريح + طلب من loopback.
- التطبيق **يرفض الإقلاع** إن فُعّل في staging/production (`app/config.py`)، وقيم الأسرار التطويرية مرفوضة هناك أيضًا.

## اختلاف عن أسماء المتغيرات في المواصفة

- `SUPABASE_AUTH_PUBLIC_KEY` → **`SUPABASE_PUBLISHABLE_KEY`** (اسم Supabase الحالي للمفتاح المنشور). مفاتيح التحقق تُجلب من JWKS ولا تحتاج متغيرًا.
- أضيف `DATA_HASH_KEY` (بصمات HMAC لمنع التواصل وجهات الاتصال؛ لا يُدوّر دون ترحيل)، و`SUPABASE_JWT_AUDIENCE`، و`SOURCE_FETCH_ENABLED`.
