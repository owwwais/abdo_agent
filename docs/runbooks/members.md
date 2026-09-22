# إضافة المؤسسين كأعضاء

التسجيل العام معطل. الخطوات لكل مؤسس:

1. في لوحة Supabase: Authentication → Users → **Invite user** بالبريد. تأكد أن «Allow new users to sign up» معطل، وأن مفاتيح توقيع JWT غير المتماثلة مفعلة (JWT Signing Keys).
2. انسخ `User UID` من اللوحة.
3. اربطه بعضوية:

```bash
uv run python scripts/add_member.py --workspace-name "اسم الشركة" \
  --auth-user-id <UID> --email <البريد> --name "الاسم" --role owner --phone-region SA
```

`--phone-region` يضبط الدولة الافتراضية لتطبيع أرقام الهاتف المحلية (مثل 05xxxxxxxx). السكربت idempotent ويُسجل في `audit_log`.

لتعطيل عضو: حدّث `memberships.status = 'disabled'`؛ جلساته تُرفض فورًا في الطلب التالي. شاشة إدارة الأعضاء مؤجلة.
