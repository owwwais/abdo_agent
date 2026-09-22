"""عقد الموصلات ونماذج نتائجها. كل نتيجة تحمل مصدرها ووقت جلبها وعدد الطلبات والتكلفة المعروفة."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

CheckStatus = Literal["succeeded", "needs_setup", "restricted", "failed"]


@dataclass(frozen=True)
class SourceConfig:
    """لقطة ثابتة من إعداد المصدر تُمرر للموصل؛ لا تحتوي أسرارًا، فقط مرجع السر."""

    source_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: str
    connector_key: str
    url: str | None
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    max_pages: int
    max_records: int
    max_requests: int
    timeout_seconds: int
    credential_ref: str | None
    store_raw: bool
    retention_days: int
    config_version: int
    extra: dict[str, Any] = field(default_factory=dict)


class ConnectorIssue(BaseModel):
    code: str
    message: str
    url: str | None = None


class SampleRecord(BaseModel):
    source_record_id: str
    url: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    excerpt: str | None = None


class ValidationReport(BaseModel):
    status: CheckStatus
    code: str
    message: str
    next_step: str | None = None


class SampleResult(BaseModel):
    status: CheckStatus
    code: str
    message: str
    next_step: str | None = None
    records: list[SampleRecord] = Field(default_factory=list)
    fields_found: list[str] = Field(default_factory=list)
    fields_missing: list[str] = Field(default_factory=list)
    issues: list[ConnectorIssue] = Field(default_factory=list)
    pages_fetched: int = 0
    request_count: int = 0
    bytes_fetched: int = 0
    # None = تكلفة غير معروفة؛ Decimal("0") = مجاني معروف.
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retention_note: str | None = None
    cursor: str | None = None
    synthetic: bool = False


class NotReadyError(RuntimeError):
    """وظيفة موصل لم تُنفذ بعد في هذه المرحلة؛ لا تُعاد نتائج مختلقة بدلًا منها."""


class SourceConnector(Protocol):
    key: str

    async def validate(self, config: SourceConfig) -> ValidationReport: ...

    async def sample(self, config: SourceConfig) -> SampleResult: ...


# الحقول التي يحاول الفحص استخراجها لتوضيح الناقص للمالك.
EXPECTED_FIELDS = ("name", "description", "website", "phone", "email", "category")
FIELD_LABELS = {
    "name": "الاسم",
    "description": "الوصف",
    "website": "الموقع",
    "phone": "الهاتف",
    "email": "البريد",
    "category": "النشاط/الفئة",
    "published_at": "تاريخ النشر",
    "link": "الرابط",
}
