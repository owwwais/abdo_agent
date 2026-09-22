"""موصلات لا تتصل بالشبكة: الإدخال اليدوي، وFakeSearch الاصطناعي المعلن، والموصلات غير المهيأة."""

from __future__ import annotations

from decimal import Decimal

from app.connectors.base import SampleResult, SourceConfig, ValidationReport


class ManualConnector:
    """مقابلات وإحالات وLinkedIn: إدخال بشري فقط، لا جلب آلي ولا تسجيل دخول."""

    key = "manual"

    async def validate(self, config: SourceConfig) -> ValidationReport:
        return ValidationReport(
            status="succeeded",
            code="manual_only",
            message="مصدر إدخال يدوي؛ لا اتصال شبكي ولا جلب آلي",
        )

    async def sample(self, config: SourceConfig) -> SampleResult:
        note = (
            "LinkedIn: إدخال ومراجعة بشرية فقط؛ لا تسجيل دخول آلي ولا scraping"
            if config.kind == "linkedin"
            else "الحقول تُدخل يدويًا أو عبر استيراد CSV مع معاينة"
        )
        return SampleResult(
            status="succeeded",
            code="manual_only",
            message=note,
            cost_amount=Decimal("0"),
        )


# نتائج اصطناعية معلنة: نطاقات example محجوزة (RFC 2606) وأسماء خيالية.
_FAKE_RESULTS = (
    {
        "name": "[اصطناعي] مجمع عيادات الواحة",
        "website": "https://clinic-waha.example",
        "description": "عيادات أسنان وجلدية؛ الحجز عبر الهاتف فقط حسب الصفحة الاصطناعية",
        "category": "عيادات",
        "phone": "+966500000001",
    },
    {
        "name": "[اصطناعي] متجر بن المدينة",
        "website": "https://bun-madina.example",
        "description": "متجر إلكتروني لبيع القهوة المختصة",
        "category": "متاجر",
    },
    {
        "name": "[اصطناعي] شقق النخيل المفروشة",
        "website": "https://nakheel-stays.example",
        "description": "مشغّل لعدة وحدات إقامة قصيرة المدى",
        "category": "إقامة قصيرة",
        "email": "info@nakheel-stays.example",
    },
)


class UnconfiguredConnector:
    """موصل معروف لكنه غير مهيأ: يعيد needs_setup مع الخطوة التالية، دون نتائج مختلقة."""

    def __init__(self, key: str, message: str, next_step: str) -> None:
        self.key = key
        self._message = message
        self._next_step = next_step

    async def validate(self, config: SourceConfig) -> ValidationReport:
        return ValidationReport(
            status="needs_setup",
            code="not_configured",
            message=self._message,
            next_step=self._next_step,
        )

    async def sample(self, config: SourceConfig) -> SampleResult:
        report = await self.validate(config)
        return SampleResult(**report.model_dump())
