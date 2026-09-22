"""استخراج حقول محدود من HTML دون تنفيذ JavaScript. لا يُحفظ HTML الخام."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WS_RE = re.compile(r"\s+")
_LOGIN_HINTS = ("تسجيل الدخول", "login", "sign in", "log in", "كلمة المرور", "password")


def clean(text: str | None, limit: int = 300) -> str:
    text = _WS_RE.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def strip_html(fragment: str) -> str:
    return BeautifulSoup(fragment or "", "html.parser").get_text(" ")


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


@dataclass
class ParsedPage:
    url: str
    fields: dict[str, str] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    login_wall: bool = False
    text_length: int = 0


def _json_ld_org(soup: BeautifulSoup) -> dict[str, Any]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or ""
        if len(raw) > 100_000:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = (
            data
            if isinstance(data, list)
            else data.get("@graph", [data])
            if isinstance(data, dict)
            else []
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(
                isinstance(k, str)
                and (
                    k in ("Organization", "LocalBusiness")
                    or k.endswith("Business")
                    or k in ("Store", "Hotel", "MedicalClinic", "Dentist")
                )
                for k in kinds
            ):
                return item
    return {}


def parse_html(html: str, base_url: str) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    org = _json_ld_org(soup)
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()

    page = ParsedPage(url=canonical_url(base_url))

    def meta(**attrs: str) -> str:
        wanted: dict[str, Any] = dict(attrs)
        tag = soup.find("meta", attrs=wanted)
        content = tag.get("content") if tag else None
        return content if isinstance(content, str) else ""

    h1 = soup.find("h1")
    title_tag = soup.find("title")
    name = (
        (org.get("name") if isinstance(org.get("name"), str) else "")
        or meta(property="og:site_name")
        or (h1.get_text(" ") if h1 else "")
        or (title_tag.get_text(" ") if title_tag else "")
    )
    if name:
        page.fields["name"] = clean(name, 200)

    description = (
        (org.get("description") if isinstance(org.get("description"), str) else "")
        or meta(name="description")
        or meta(property="og:description")
    )
    if not description:
        for p in soup.find_all("p"):
            text = clean(p.get_text(" "), 300)
            if len(text) >= 40:
                description = text
                break
    if description:
        page.fields["description"] = clean(description, 300)

    org_type = org.get("@type")
    if isinstance(org_type, str) and org_type not in ("Organization",):
        page.fields["category"] = org_type
    else:
        keywords = meta(name="keywords")
        if keywords:
            page.fields["category"] = clean(
                ", ".join(k.strip() for k in keywords.split(",")[:3]), 120
            )

    if isinstance(org.get("url"), str):
        page.fields["website"] = org["url"][:300]
    else:
        page.fields["website"] = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"

    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        low = href.lower()
        if low.startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if _EMAIL_RE.fullmatch(email) and email not in page.emails:
                page.emails.append(email)
        elif low.startswith("tel:"):
            phone = re.sub(r"[^\d+]", "", href[4:])
            if 7 <= len(phone) <= 16 and phone not in page.phones:
                page.phones.append(phone)
        elif not low.startswith(("javascript:", "data:", "#")):
            absolute = urljoin(base_url, href)
            if absolute.startswith(("http://", "https://")):
                c = canonical_url(absolute)
                if c not in page.links and c != page.url:
                    page.links.append(c)

    body_text = soup.get_text(" ")
    page.text_length = len(_WS_RE.sub(" ", body_text).strip())
    for email in _EMAIL_RE.findall(body_text):
        if email not in page.emails and len(page.emails) < 5:
            page.emails.append(email)
    if isinstance(org.get("email"), str) and org["email"] not in page.emails:
        page.emails.insert(0, org["email"].removeprefix("mailto:"))
    if isinstance(org.get("telephone"), str):
        phone = re.sub(r"[^\d+]", "", org["telephone"])
        if phone and phone not in page.phones:
            page.phones.insert(0, phone)
    if page.emails:
        page.fields["email"] = page.emails[0]
    if page.phones:
        page.fields["phone"] = page.phones[0]

    has_password = soup.find("input", attrs={"type": "password"}) is not None
    lowered = body_text.lower()
    page.login_wall = (
        has_password and page.text_length < 2000 and any(h in lowered for h in _LOGIN_HINTS)
    )
    page.links = page.links[:200]
    return page


def link_texts(html: str, base_url: str, limit: int = 200) -> list[tuple[str, str]]:
    """روابط الصفحة مع نصوصها (لقوائم الأدلة): [(نص، رابط مطلق)] بلا تكرار."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        url = canonical_url(absolute)
        text = clean(a.get_text(" "), 200)
        if url in seen or not text:
            continue
        seen.add(url)
        out.append((text, url))
        if len(out) >= limit:
            break
    return out
