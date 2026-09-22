from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.connectors.base import FIELD_LABELS
from app.connectors.registry import SOURCE_KINDS

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
_TZ = ZoneInfo("Asia/Riyadh")

LABELS: dict[str, dict[str, str]] = {
    "product": {"draft": "مسودة", "active": "نشط", "paused": "موقوف مؤقتًا", "archived": "مؤرشف"},
    "source": {
        "new": "جديد",
        "validating": "قيد الفحص",
        "sample_ready": "العينة جاهزة للمراجعة",
        "active": "نشط",
        "needs_setup": "يحتاج إعدادًا",
        "restricted": "مقيّد",
        "failed": "فشل",
        "paused": "موقوف",
    },
    "check": {
        "succeeded": "ناجح",
        "needs_setup": "يحتاج إعدادًا",
        "restricted": "مقيّد",
        "failed": "فشل",
    },
    "company": {"active": "نشطة", "needs_review": "تحتاج مراجعة", "archived": "مؤرشفة"},
    "import_action": {
        "create": "إنشاء شركة",
        "link": "ربط بشركة موجودة",
        "review": "إنشاء مع مراجعة تكرار محتمل",
        "skip_error": "مرفوض: خطأ في الصف",
        "skip_suppressed": "مستبعد: منع تواصل",
        "skip_duplicate_in_file": "مكرر في الملف",
        "skip_conflict": "تعارض معرفات؛ لم يُطبق",
    },
    "batch": {"preview": "معاينة", "committed": "معتمد", "canceled": "ملغى"},
    "price": {"needs_review": "يحتاج مراجعة", "approved": "معتمد"},
    "role": {"owner": "مالك", "reviewer": "مراجع"},
    "segment": {"active": "نشطة", "archived": "مؤرشفة"},
    "job": {
        "queued": "في الطابور",
        "running": "قيد التنفيذ",
        "retry_scheduled": "إعادة محاولة مجدولة",
        "completed": "اكتملت",
        "failed": "فشلت",
        "canceled": "أُلغيت",
    },
    "kind": {k: v.label for k, v in SOURCE_KINDS.items()},
    "field": FIELD_LABELS,
    "identifier": {
        "domain": "النطاق",
        "platform_account": "حساب/متجر على منصة",
        "cr_number": "السجل التجاري",
        "phone": "هاتف (معرف ضعيف)",
        "name": "الاسم المطبع (معرف ضعيف)",
    },
    "channel": {"email": "بريد", "phone": "هاتف"},
}

TONES = {
    "active": "ok",
    "succeeded": "ok",
    "committed": "ok",
    "completed": "ok",
    "approved": "ok",
    "sample_ready": "info",
    "validating": "info",
    "queued": "info",
    "running": "info",
    "preview": "info",
    "new": "muted",
    "draft": "muted",
    "archived": "muted",
    "canceled": "muted",
    "paused": "warn",
    "needs_setup": "warn",
    "needs_review": "warn",
    "retry_scheduled": "warn",
    "restricted": "bad",
    "failed": "bad",
}

FLASH = {
    "product_created": "أُنشئ المنتج كمسودة. أكمل الوصف والفئات ثم فعّله.",
    "product_saved": "حُفظت التعديلات.",
    "product_status": "تغيّرت حالة المنتج.",
    "segment_created": "أُنشئت الفئة.",
    "segment_saved": "حُفظت الفئة.",
    "source_created": "أُنشئ المصدر بحالة «جديد». شغّل فحص العينة ثم راجعه قبل التفعيل.",
    "source_saved": "حُفظ الإعداد.",
    "source_test": "جُدول فحص العينة. يعمل عندما يكون العامل (worker) مشغلًا.",
    "source_active": "فُعّل المصدر بعد تأكيدك لسياسة الاستخدام والتخزين.",
    "source_paused": "أُوقف المصدر.",
    "import_committed": "اعتُمدت الدفعة.",
    "import_canceled": "أُلغيت الدفعة وحُذفت بيانات صفوفها.",
    "manual_added": "سُجلت الجهة.",
}


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")


def label(group: str, key: str | None) -> str:
    if key is None:
        return "—"
    return LABELS.get(group, {}).get(key, key)


templates.env.filters["dt"] = fmt_dt
templates.env.globals["label"] = label
templates.env.globals["tone"] = lambda key: TONES.get(key or "", "muted")


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    settings = request.app.state.settings
    context.setdefault("principal", getattr(request.state, "principal", None))
    context.setdefault("flash", FLASH.get(request.query_params.get("ok", ""), None))
    context["env"] = settings.app_env.value
    context["request_path"] = request.url.path
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
