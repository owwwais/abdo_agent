from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import Forbidden, InvalidInput, NotFound
from app.auth.sessions import Principal
from app.connectors.netguard import BlockedUrl, check_url


def require_owner(p: Principal) -> None:
    if not p.is_owner:
        raise Forbidden("هذا الإجراء للمالك فقط", code="owner_only")


async def get_scoped[T](
    db: AsyncSession,
    model: type[T],
    workspace_id: uuid.UUID,
    obj_id: uuid.UUID,
    *,
    lock: bool = False,
) -> T:
    """يجلب سجلًا مملوكًا للـworkspace أو يرفع NotFound (دون كشف وجوده في workspace آخر)."""
    q = select(model).where(model.id == obj_id, model.workspace_id == workspace_id)  # type: ignore[attr-defined]
    if lock:
        q = q.with_for_update()
    obj = (await db.execute(q)).scalar_one_or_none()
    if obj is None:
        raise NotFound()
    return obj


def _strip(v: str) -> str:
    return v.strip()


def _clean_list(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return seen


Text = Annotated[str, AfterValidator(_strip)]
TextList = Annotated[
    list[Annotated[str, Field(max_length=500)]], AfterValidator(_clean_list), Field(max_length=50)
]


def _public_url(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    try:
        return check_url(v.strip()).url
    except BlockedUrl as exc:
        raise ValueError(exc.message) from exc


PublicUrl = Annotated[str | None, AfterValidator(_public_url)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=5000)


def lines_to_list(text: str | None) -> list[str]:
    """حقل نصي متعدد الأسطر في النماذج → قائمة."""
    return [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]


_ERROR_TYPES_AR = {
    "missing": "حقل مطلوب",
    "string_too_short": "النص قصير جدًا أو فارغ",
    "string_too_long": "النص أطول من المسموح",
    "too_long": "عدد العناصر أكبر من المسموح",
    "too_short": "عدد العناصر أقل من المطلوب",
    "int_parsing": "يجب أن يكون رقمًا صحيحًا",
    "int_type": "يجب أن يكون رقمًا صحيحًا",
    "greater_than_equal": "القيمة أقل من الحد الأدنى",
    "less_than_equal": "القيمة أكبر من الحد الأعلى",
    "greater_than": "القيمة أقل من المسموح",
    "literal_error": "قيمة غير مسموحة",
    "enum": "قيمة غير مسموحة",
    "extra_forbidden": "حقل غير متوقع",
    "uuid_parsing": "معرف غير صالح",
    "uuid_type": "معرف غير صالح",
    "bool_parsing": "قيمة منطقية غير صالحة",
    "json_invalid": "JSON غير صالح",
    "model_attributes_type": "صيغة غير صالحة",
    "dict_type": "صيغة غير صالحة",
    "list_type": "يجب أن تكون قائمة",
    "string_type": "يجب أن يكون نصًا",
    "string_pattern_mismatch": "الصيغة غير صالحة",
}


def validation_details(exc: Exception) -> dict[str, Any]:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return {}
    fields: dict[str, str] = {}
    for err in errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        kind = err.get("type", "")
        if kind == "value_error":
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
        else:
            msg = _ERROR_TYPES_AR.get(kind, "قيمة غير صالحة")
        fields.setdefault(loc or "_", msg)
    return {"fields": fields}


def invalid(message: str, **fields: str) -> InvalidInput:
    return InvalidInput(message, details={"fields": fields} if fields else None)
