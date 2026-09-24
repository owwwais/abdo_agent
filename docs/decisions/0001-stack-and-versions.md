# 0001 — المكدس والإصدارات المثبتة

التاريخ: 2026-09-22 · الحالة: معتمد للبناء

## القرار

- **Python 3.12** (مثبت في `.python-version`). السبب: مستقر ومدعوم من كل المكتبات، و`pgserver` (قاعدة PostgreSQL محلية دون Docker) لا يوفر عجلات إلا حتى cp312. صورة الإنتاج `python:3.12-slim`.
- الاعتماديات مثبتة في `uv.lock`. أهم الإصدارات وقت التنفيذ:

| الحزمة | الإصدار | ملاحظة تحقق |
|---|---|---|
| fastapi | 0.141.1 | ما زال يوفر `@app.exception_handler` و`@app.middleware` بنفسه |
| starlette | 1.6.0 | 1.0 أزال `on_startup/on_shutdown` وتوقيع `TemplateResponse(name, ctx)` → نستخدم `lifespan` و`TemplateResponse(request, name, ctx)` (ملاحظات الإصدار الرسمية) |
| sqlalchemy | 2.0.54 | async مع psycopg 3؛ `eager_defaults` لتجنب التحميل الكسول بعد التحديث |
| alembic | 1.20.0 | ترحيلات مرحلية لكل milestone |
| psycopg[binary] | 3.3.6 | غير المتزامن يحتاج `SelectorEventLoop` في Windows (`app/db/session.py: loop_factory`) |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 | `env_ignore_empty=True` كي لا تفشل القيم الفارغة في `.env` |
| httpx | 0.28.1 | امتداد `sni_hostname` للاتصال بعنوان IP مفحوص مع تحقق الشهادة للاسم الأصلي (تحقق من مصدر httpcore) |
| pyjwt[crypto] | 2.14.0 | `PyJWKSet.from_dict`، التحقق من ES256/RS256 |
| beautifulsoup4 / defusedxml / phonenumbers | 4.15.0 / 0.7.1 / 9.0.39 | تحليل HTML دون تنفيذ JS، XML آمن، تطبيع الهاتف |
| pytest-asyncio | 1.4.0 | حلقة Windows عبر hook `pytest_asyncio_loop_factories` (تجاوز `event_loop_policy` مهمل) |

- **LangGraph غير مثبت بعد**: يدخل في M2 مع التحقق من الإصدار والـcheckpointer المتوافق وقتها، ولا يُجمع مع PydanticAI.
- لا اشتراك في منصة مراقبة تجارية.

## واجهات خارجية (تحقق 2026-09-24)

| الواجهة | ما نستخدمه | المرجع |
|---|---|---|
| Google Places API (New) | `POST places:searchText`، `X-Goog-FieldMask` إلزامي، `pageSize` ≤ 20، 60 نتيجة كحد أقصى | [Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search) |
| Tavily | `POST /search`، `Authorization: Bearer`، `search_depth=basic` (رصيد واحد)، 432/433 = نفاد الخطة | [Search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search) |
| Brave Search | `GET /res/v1/web/search`، `X-Subscription-Token` | [Brave Search API](https://brave.com/search/api/) |

## المصادر الرسمية التي روجعت

- Supabase: [JWT signing keys](https://supabase.com/docs/guides/auth/signing-keys)، [JWTs](https://supabase.com/docs/guides/auth/jwts)، [API keys](https://supabase.com/docs/guides/api/api-keys).
- Starlette release notes (مستودع Kludex/starlette، `docs/release-notes.md`).
- Render [Blueprint spec](https://render.com/docs/blueprint-spec): `autoDeployTrigger` بدل `autoDeploy` المهمل، `preDeployCommand`، `envVarGroups`.
- مصدر pytest-asyncio 1.4 المثبت (hook مصانع الحلقات).
