"""تطبيع الأسماء والنطاقات والهواتف والبريد لأغراض المطابقة، دون دمج مفرط.

قواعد: الاسم وحده لا يكفي للدمج؛ النطاق الموثق والسجل التجاري معرفات قوية؛ الهاتف ضعيف (فروع
تتشارك الأرقام)؛ دومين منصة متعددة المستأجرين لا يحدد متجرًا بعينه إلا مع مسار المتجر؛ لا تُحذف
تنويعات البريد (النقاط وعلامة +) كي لا يُدمج شخصان.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

import phonenumbers

from app.connectors.netguard import BlockedUrl, check_url

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_LEGAL_WORDS = {
    "شركه",
    "مؤسسه",
    "موسسه",
    "مجموعه",
    "المحدوده",
    "محدوده",
    "ذ",
    "م",
    "للتجاره",
    "co",
    "company",
    "ltd",
    "llc",
    "inc",
    "est",
    "group",
    "corp",
    "the",
}
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")

# منصات يحدد فيها المسار الأول الحساب/المتجر (salla.sa/store-name).
PATH_PLATFORMS = {
    "salla.sa",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "snapchat.com",
    "youtube.com",
    "linkedin.com",
    "t.me",
    "wa.me",
    "linktr.ee",
    "booking.com",
    "airbnb.com",
    "gathern.co",
    "github.com",
    "behance.net",
}
# منصات يحدد فيها النطاق الفرعي المستأجر؛ الجذر نفسه ليس معرفًا لأي منشأة.
SUBDOMAIN_PLATFORMS = {
    "myshopify.com",
    "zid.store",
    "salla.site",
    "wixsite.com",
    "blogspot.com",
    "wordpress.com",
    "business.site",
    "github.io",
    "netlify.app",
    "vercel.app",
    "webflow.io",
    "square.site",
}
# روابط خرائط واختصارات لا تصلح معرّفًا للمنشأة.
NON_IDENTIFYING = {"google.com", "maps.google.com", "goo.gl", "maps.app.goo.gl", "bit.ly", "g.page"}
FREEMAIL = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "outlook.sa",
    "yandex.com",
}
_GENERIC_SEGMENTS = {
    "",
    "p",
    "reel",
    "share",
    "sharer",
    "watch",
    "hashtag",
    "explore",
    "search",
    "maps",
    "rooms",
    "hotel",
}

COUNTRY_ALIASES = {
    "sa": "SA",
    "ksa": "SA",
    "saudi arabia": "SA",
    "السعودية": "SA",
    "المملكة العربية السعودية": "SA",
    "ae": "AE",
    "uae": "AE",
    "الإمارات": "AE",
    "الامارات": "AE",
    "kw": "KW",
    "الكويت": "KW",
    "bh": "BH",
    "البحرين": "BH",
    "qa": "QA",
    "قطر": "QA",
    "om": "OM",
    "عمان": "OM",
    "عُمان": "OM",
    "eg": "EG",
    "مصر": "EG",
    "jo": "JO",
    "الأردن": "JO",
    "الاردن": "JO",
}


@dataclass(frozen=True)
class Identifier:
    kind: str  # domain | platform_account | cr_number | phone | name
    value: str
    strength: str  # strong | weak


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = _TASHKEEL.sub("", s).replace("ـ", "")
    s = re.sub("[إأآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = _NON_WORD.sub(" ", s)
    tokens = [t for t in s.split() if t not in _LEGAL_WORDS]
    return " ".join(tokens)[:300]


def _root(host: str, platforms: set[str]) -> str | None:
    for p in platforms:
        if host == p or host.endswith("." + p):
            return p
    return None


def website_identifier(raw: str) -> tuple[Identifier | None, str | None]:
    """يعيد (المعرف، النطاق المعروض). يرفع ValueError برسالة عربية لرابط غير صالح أو داخلي."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    url = raw if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I) else f"https://{raw}"
    try:
        checked = check_url(url)
    except BlockedUrl as exc:
        raise ValueError(exc.message) from exc
    host = checked.host.removeprefix("www.").removeprefix("m.")
    if _root(host, NON_IDENTIFYING):
        return None, None
    path_root = _root(host, PATH_PLATFORMS)
    if path_root:
        segment = urlsplit(url).path.strip("/").split("/")[0].lower().lstrip("@")
        if segment in _GENERIC_SEGMENTS:
            return None, None
        return Identifier("platform_account", f"{path_root}/{segment}", "strong"), None
    sub_root = _root(host, SUBDOMAIN_PLATFORMS)
    if sub_root and host == sub_root:
        return None, None
    return Identifier("domain", host, "strong"), host


def normalize_cr(raw: str) -> Identifier | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) != 10:
        raise ValueError("رقم السجل التجاري/الموحد يتكون من 10 أرقام")
    return Identifier("cr_number", digits, "strong")


def normalize_country(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    code = COUNTRY_ALIASES.get(raw.lower()) or (
        raw.upper() if re.fullmatch(r"[A-Za-z]{2}", raw) else None
    )
    if code is None:
        raise ValueError("الدولة غير معروفة؛ استخدم رمزًا من حرفين مثل SA")
    return code


def normalize_phone(raw: str, region: str | None) -> tuple[str, bool]:
    """يعيد (القيمة، هل طُبعت بثقة). بلا دولة معروفة يبقى الرقم غير مؤكد."""
    raw = (raw or "").strip()
    digits = re.sub(r"[^\d+]", "", raw)
    if len(re.sub(r"\D", "", digits)) < 7:
        raise ValueError("رقم الهاتف قصير أو غير صالح")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+") or region:
        try:
            parsed = phonenumbers.parse(digits, None if digits.startswith("+") else region)
        except phonenumbers.NumberParseException:
            return "raw:" + re.sub(r"\D", "", digits), False
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), True
    return "raw:" + re.sub(r"\D", "", digits), False


def normalize_email(raw: str) -> str:
    raw = (raw or "").strip()
    if not _EMAIL_RE.match(raw):
        raise ValueError("البريد الإلكتروني غير صالح")
    local, domain = raw.rsplit("@", 1)
    # الجزء المحلي يُحفظ كما هو؛ فقط النطاق بأحرف صغيرة.
    return f"{local}@{domain.lower()}"


def email_domain(email: str) -> str | None:
    domain = email.rsplit("@", 1)[-1].lower()
    return None if domain in FREEMAIL else domain
