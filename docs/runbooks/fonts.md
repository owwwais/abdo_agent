# تضمين خط Readex Pro محليًا

الواجهة تستخدم `"Readex Pro"` إن كان مثبتًا، وإلا بدائل النظام العربية (Noto Sans Arabic، Segoe UI، Tahoma). لم يُنزَّل ملف الخط ضمن هذا البناء.

لتضمينه (ترخيص SIL Open Font License 1.1):

1. نزّل الخط من مصدره الرسمي (Google Fonts أو مستودع المصمم) واحفظ `ReadexPro-Variable.woff2` في `app/static/fonts/` مع ملف `OFL.txt`.
2. أضف في أعلى `app/static/app.css`:

```css
@font-face {
  font-family: "Readex Pro";
  src: url("/static/fonts/ReadexPro-Variable.woff2") format("woff2");
  font-weight: 200 700;
  font-display: swap;
}
```

لا يلزم تعديل CSP لأن الخط من المصدر نفسه.
