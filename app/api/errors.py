"""أخطاء التطبيق: رمز ثابت + رسالة عربية مفهومة + حالة HTTP مناسبة."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    message = "طلب غير صالح"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class Unauthenticated(AppError):
    status_code = 401
    code = "unauthenticated"
    message = "يلزم تسجيل الدخول"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"
    message = "لا تملك صلاحية هذا الإجراء"


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    message = "العنصر غير موجود"


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "تعارض مع الحالة الحالية"


class VersionConflict(Conflict):
    code = "version_conflict"
    message = "تغيّر السجل منذ فتحه. حدّث الصفحة وأعد المحاولة"


class InvalidInput(AppError):
    status_code = 422
    code = "invalid_input"
    message = "بيانات غير صالحة"


class CsrfFailed(AppError):
    status_code = 403
    code = "csrf_failed"
    message = "انتهت صلاحية النموذج أو لم يُتحقق من مصدره. حدّث الصفحة وأعد المحاولة"
