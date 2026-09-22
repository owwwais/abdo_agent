"""فحص سياسة المسودات برمجيًا قبل عرضها للاعتماد: لا خصائص غير معتمدة، لا سعر غير معتمد، لا أرقام أو روابط
أو ادعاءات بلا دليل. النموذج لا يحكم على نفسه (SPEC §8.1، §15)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from app.db.models import Product

_DIGITS = re.compile(r"[0-9٠-٩][0-9٠-٩.,٫]*")
_PRICE = re.compile(r"(ريال|ر\.س|SAR|USD|\$|دولار|سعر|أسعار|اسعار|خصم|مجان|%)", re.I)
_CLAIMS = re.compile(
    r"(مضمون|نضمن|ضمان|الأفضل|الافضل|رقم 1|الأول في|100 ?%|بلا منافس|أكثر من \d+ عميل)", re.I
)
_URL = re.compile(r"https?://[^\s)]+", re.I)
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")


@dataclass
class Finding:
    code: str
    message: str
    blocking: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_num(s: str) -> str:
    return s.translate(_ARABIC_DIGITS).replace(",", "").strip(".")


def check_draft(
    *,
    subject: str,
    body: str,
    product: Product,
    capabilities_used: list[str],
    support_texts: list[str],
    allowed_urls: list[str],
) -> list[Finding]:
    findings: list[Finding] = []
    if not subject.strip():
        findings.append(Finding("subject_missing", "الموضوع فارغ", True))
    elif len(subject) > 120:
        findings.append(Finding("subject_long", "الموضوع أطول من 120 حرفًا", True))
    words = len(body.split())
    if words < 45 or words > 140:
        findings.append(Finding("length", f"طول الرسالة {words} كلمة (التوجيه 60–120)", False))
    approved = {c.strip() for c in product.capabilities}
    for cap in capabilities_used:
        if cap.strip() not in approved:
            findings.append(Finding("unapproved_capability", f"خاصية غير معتمدة: {cap}", True))
    for cap in product.unavailable_capabilities:
        if cap.strip() and cap.strip() in body:
            findings.append(Finding("unavailable_capability", f"ذكر خاصية غير متاحة: {cap}", True))
    support = " ".join(
        [*support_texts, product.problem, product.summary, " ".join(product.capabilities)]
    )
    if _PRICE.search(body):
        if product.price_status != "approved":
            findings.append(
                Finding("price_not_approved", "الرسالة تذكر سعرًا أو خصمًا والسعر غير معتمد", True)
            )
        else:
            support += " " + product.price_text
    support_nums = {_norm_num(n) for n in _DIGITS.findall(support)}
    for num in _DIGITS.findall(body):
        n = _norm_num(num)
        if n and n not in support_nums:
            findings.append(Finding("unsupported_number", f"رقم بلا دليل في السياق: {num}", True))
    for m in _CLAIMS.finditer(body):
        findings.append(Finding("unsupported_claim", f"ادعاء غير مدعوم: {m.group(0)}", True))
    allowed = {u.rstrip("/").lower() for u in allowed_urls if u}
    for url in _URL.findall(body):
        if url.rstrip("/.،").lower() not in allowed:
            findings.append(Finding("unknown_link", f"رابط غير معتمد: {url}", True))
    return findings


def blocking(findings: list[dict[str, Any]]) -> bool:
    return any(f.get("blocking") for f in findings)
