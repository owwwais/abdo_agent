"""نقل البريد: إرسال SMTP وقراءة IMAP (يعمل مع بريد Hostinger وأي مزود قياسي)، وصندوق اصطناعي للاختبار.

الإرسال يمر بمراحل صريحة؛ فشل قبل مرحلة DATA يعني أن الرسالة لم تُسلّم (آمن للإعادة)، وفشل أثناءها
أو بعدها يعني «تسليم غير معروف» (unknown_delivery) ولا يُعاد الإرسال آليًا (SPEC §8.4).
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime
from typing import Any, Literal, Protocol

from bs4 import BeautifulSoup

TIMEOUT = 30
MAX_INBOUND_BYTES = 1_000_000
MAX_FETCH_PER_SYNC = 50


@dataclass
class OutgoingMail:
    from_address: str
    from_name: str
    to_address: str
    subject: str
    body: str
    reply_to: str = ""
    in_reply_to: str | None = None
    references: str = ""
    message_id: str | None = None


@dataclass
class SendOutcome:
    status: Literal["sent", "failed", "unknown_delivery"]
    message_id: str
    detail: str = ""
    retryable: bool = False
    sent_copy_saved: bool = False


@dataclass
class InboundMail:
    uid: str
    message_id: str | None
    in_reply_to: str | None
    references: str
    from_address: str
    from_name: str
    to_address: str
    subject: str
    body_text: str
    date: datetime
    auto_submitted: bool = False
    is_bounce: bool = False
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class TestReport:
    items: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(ok for _, ok, _ in self.items)

    def summary(self) -> str:
        return " | ".join(f"{name}: {'✓' if ok else '✗'} {msg}" for name, ok, msg in self.items)


class Mailbox(Protocol):
    name: str

    async def send(self, mail: OutgoingMail) -> SendOutcome: ...

    async def fetch_new(
        self, cursor: dict[str, Any]
    ) -> tuple[list[InboundMail], dict[str, Any]]: ...

    async def test(self) -> TestReport: ...


@dataclass
class SmtpImapSettings:
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    imap_enabled: bool
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_folder: str
    sent_folder: str


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else "localhost"


def build_message(mail: OutgoingMail) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = (
        formataddr((mail.from_name, mail.from_address)) if mail.from_name else mail.from_address
    )
    msg["To"] = mail.to_address
    msg["Subject"] = mail.subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = mail.message_id or make_msgid(domain=domain_of(mail.from_address))
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    if mail.in_reply_to:
        msg["In-Reply-To"] = mail.in_reply_to
        msg["References"] = (mail.references + " " + mail.in_reply_to).strip()
    msg.set_content(mail.body, charset="utf-8")
    return msg


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text("\n")


def strip_quoted(text: str) -> str:
    """يزيل النص المقتبس من الرد لتقليل ما يُخزن ويُرسل للنموذج."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            continue
        if re.match(r"^(On .+ wrote:|في .+ كتب:|-----Original Message-----|From: )", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_inbound(uid: str, raw: bytes) -> InboundMail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", errors="replace")  # type: ignore[union-attr]
        body = _html_to_text(content) if part.get_content_type() == "text/html" else str(content)
    from_pairs = getaddresses([str(msg.get("From", ""))])
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")
    to_pairs = getaddresses([str(msg.get("To", ""))])
    auto = str(msg.get("Auto-Submitted", "no")).lower()
    subject = str(msg.get("Subject", ""))
    is_auto = (
        (auto and auto != "no")
        or bool(msg.get("X-Autoreply"))
        or bool(msg.get("X-Autorespond"))
        or str(msg.get("Precedence", "")).lower() in ("auto_reply", "bulk", "junk")
        or bool(
            re.search(
                r"(?i)(auto(matic)? ?reply|out of (the )?office|رد تلقائي|خارج المكتب)", subject
            )
        )
    )
    ctype = msg.get_content_type()
    is_bounce = ctype == "multipart/report" or bool(
        re.match(r"(?i)^(mailer-daemon|postmaster)@", from_addr or "")
    )
    try:
        date = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else datetime.now(UTC)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    return InboundMail(
        uid=uid,
        message_id=(str(msg.get("Message-ID")).strip() or None) if msg.get("Message-ID") else None,
        in_reply_to=(str(msg.get("In-Reply-To")).strip() or None)
        if msg.get("In-Reply-To")
        else None,
        references=str(msg.get("References", "")).strip(),
        from_address=(from_addr or "").lower(),
        from_name=from_name or "",
        to_address=(to_pairs[0][1] if to_pairs else "").lower(),
        subject=subject[:500],
        body_text=strip_quoted(body)[:20000],
        date=date,
        auto_submitted=is_auto,
        is_bounce=is_bounce,
    )


class SmtpImapMailbox:
    name = "smtp"

    def __init__(self, s: SmtpImapSettings) -> None:
        self.s = s

    # ---------------------------------------------------------------- SMTP

    def _smtp_connect(self) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if self.s.smtp_security == "ssl":
            conn: smtplib.SMTP = smtplib.SMTP_SSL(
                self.s.smtp_host, self.s.smtp_port, timeout=TIMEOUT, context=ctx
            )
        else:
            conn = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=TIMEOUT)
            conn.ehlo()
            conn.starttls(context=ctx)
        conn.ehlo()
        conn.login(self.s.smtp_username, self.s.smtp_password)
        return conn

    def _send_sync(self, mail: OutgoingMail) -> SendOutcome:
        msg = build_message(mail)
        message_id = str(msg["Message-ID"])
        phase = "connect"
        conn: smtplib.SMTP | None = None
        try:
            conn = self._smtp_connect()
            phase = "envelope"
            code, resp = conn.mail(mail.from_address)
            if code != 250:
                return SendOutcome("failed", message_id, f"MAIL FROM رُفض ({code})")
            code, resp = conn.rcpt(mail.to_address)
            if code not in (250, 251):
                return SendOutcome("failed", message_id, f"المستلم رُفض ({code}): {resp[:200]!r}")
            phase = "data"
            code, resp = conn.data(msg.as_bytes())
            if code != 250:
                # رفض صريح من الخادم بعد DATA يعني عدم القبول.
                return SendOutcome("failed", message_id, f"الخادم رفض الرسالة ({code})")
            phase = "done"
        except smtplib.SMTPAuthenticationError:
            return SendOutcome("failed", message_id, "فشل تسجيل الدخول إلى SMTP")
        except (TimeoutError, smtplib.SMTPException, OSError) as exc:
            if phase in ("data",):
                return SendOutcome(
                    "unknown_delivery",
                    message_id,
                    f"انقطع الاتصال أثناء الإرسال: {type(exc).__name__}",
                )
            return SendOutcome(
                "failed",
                message_id,
                f"تعذر الإرسال قبل تسليم الرسالة: {type(exc).__name__}",
                retryable=True,
            )
        finally:
            if conn is not None:
                try:
                    conn.quit()
                except (smtplib.SMTPException, OSError):
                    pass
        saved = False
        if self.s.imap_enabled:
            saved = self._append_sent(msg)
        return SendOutcome("sent", message_id, "أُرسلت", sent_copy_saved=saved)

    async def send(self, mail: OutgoingMail) -> SendOutcome:
        return await asyncio.to_thread(self._send_sync, mail)

    # ---------------------------------------------------------------- IMAP

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(
            self.s.imap_host,
            self.s.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=TIMEOUT,
        )
        conn.login(self.s.imap_username, self.s.imap_password)
        return conn

    @staticmethod
    def _quote(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def detect_sent_folder(self, conn: imaplib.IMAP4_SSL) -> str | None:
        if self.s.sent_folder:
            return self.s.sent_folder
        typ, data = conn.list()
        if typ != "OK":
            return None
        names = []
        for raw in data or []:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            match = re.match(r'^\((?P<flags>[^)]*)\) "?(?P<delim>[^"]*)"? (?P<name>.+)$', line)
            if not match:
                continue
            name = match.group("name").strip().strip('"')
            if "\\Sent" in match.group("flags"):
                return name
            names.append(name)
        for candidate in ("INBOX.Sent", "Sent", "Sent Items", "INBOX.Sent Items", "Sent Messages"):
            if candidate in names:
                return candidate
        return None

    def _append_sent(self, msg: EmailMessage) -> bool:
        try:
            conn = self._imap()
            try:
                folder = self.detect_sent_folder(conn)
                if not folder:
                    return False
                typ, _ = conn.append(
                    self._quote(folder),
                    "\\Seen",
                    imaplib.Time2Internaldate(datetime.now(UTC)),
                    msg.as_bytes(),
                )
                return typ == "OK"
            finally:
                conn.logout()
        except (imaplib.IMAP4.error, OSError):
            return False

    def _fetch_sync(self, cursor: dict[str, Any]) -> tuple[list[InboundMail], dict[str, Any]]:
        conn = self._imap()
        try:
            typ, _ = conn.select(self._quote(self.s.imap_folder), readonly=True)
            if typ != "OK":
                raise OSError(f"تعذر فتح المجلد {self.s.imap_folder}")
            validity = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
            validity_s = validity.decode() if isinstance(validity, bytes) else str(validity)
            last_uid = (
                int(cursor.get("last_uid", 0)) if cursor.get("uidvalidity") == validity_s else 0
            )
            if last_uid == 0 and cursor.get("uidvalidity") != validity_s:
                # أول مزامنة أو تغير UIDVALIDITY: نبدأ من رسائل اليوم فقط لا من كل الصندوق القديم.
                since = datetime.now(UTC).strftime("%d-%b-%Y")
                typ, data = conn.uid("SEARCH", "SINCE", since)
            else:
                typ, data = conn.uid("SEARCH", f"UID {last_uid + 1}:*")
            uids = [
                int(u) for u in (data[0].split() if data and data[0] else []) if int(u) > last_uid
            ]
            uids = sorted(uids)[:MAX_FETCH_PER_SYNC]
            out: list[InboundMail] = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(RFC822.SIZE BODY.PEEK[])")
                if typ != "OK" or not parts:
                    continue
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
                if not isinstance(raw, bytes) or len(raw) > MAX_INBOUND_BYTES:
                    continue
                out.append(parse_inbound(str(uid), raw))
            new_last = max(uids) if uids else last_uid
            return out, {"uidvalidity": validity_s, "last_uid": new_last}
        finally:
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    async def fetch_new(self, cursor: dict[str, Any]) -> tuple[list[InboundMail], dict[str, Any]]:
        if not self.s.imap_enabled:
            return [], cursor
        return await asyncio.to_thread(self._fetch_sync, cursor)

    def _test_sync(self) -> TestReport:
        report = TestReport()
        try:
            conn = self._smtp_connect()
            conn.quit()
            report.items.append(
                ("SMTP", True, f"تسجيل الدخول نجح ({self.s.smtp_host}:{self.s.smtp_port})")
            )
        except smtplib.SMTPAuthenticationError:
            report.items.append(("SMTP", False, "اسم المستخدم أو كلمة المرور غير صحيحة"))
        except (smtplib.SMTPException, OSError) as exc:
            report.items.append(("SMTP", False, f"تعذر الاتصال: {type(exc).__name__}"))
        if self.s.imap_enabled:
            try:
                conn2 = self._imap()
                folder = self.detect_sent_folder(conn2)
                conn2.logout()
                note = f"مجلد المرسل: {folder}" if folder else "لم يُعثر على مجلد المرسل"
                report.items.append(("IMAP", True, f"تسجيل الدخول نجح؛ {note}"))
            except imaplib.IMAP4.error:
                report.items.append(("IMAP", False, "اسم المستخدم أو كلمة المرور غير صحيحة"))
            except OSError as exc:
                report.items.append(("IMAP", False, f"تعذر الاتصال: {type(exc).__name__}"))
        return report

    async def test(self) -> TestReport:
        return await asyncio.to_thread(self._test_sync)


class FakeMailbox:
    """صندوق اصطناعي: الرسائل المرسلة تُحفظ في الذاكرة، والواردة تُحقن من الاختبارات. لا شبكة."""

    name = "fake"
    outbox: list[EmailMessage] = []
    inbox: list[InboundMail] = []
    fail_mode: str | None = None  # None | "failed" | "unknown_delivery"

    async def send(self, mail: OutgoingMail) -> SendOutcome:
        msg = build_message(mail)
        message_id = str(msg["Message-ID"])
        if FakeMailbox.fail_mode == "unknown_delivery":
            FakeMailbox.outbox.append(msg)  # حالة «أُرسلت فعلًا ثم انقطع الرد»
            return SendOutcome("unknown_delivery", message_id, "محاكاة انقطاع بعد DATA")
        if FakeMailbox.fail_mode == "failed":
            return SendOutcome("failed", message_id, "محاكاة رفض", retryable=True)
        FakeMailbox.outbox.append(msg)
        return SendOutcome("sent", message_id, "أُرسلت (اصطناعي)")

    async def fetch_new(self, cursor: dict[str, Any]) -> tuple[list[InboundMail], dict[str, Any]]:
        last = int(cursor.get("last_uid", 0))
        new = [m for m in FakeMailbox.inbox if int(m.uid) > last]
        new_last = max([int(m.uid) for m in new], default=last)
        return new, {"uidvalidity": "fake", "last_uid": new_last}

    async def test(self) -> TestReport:
        return TestReport([("FakeMailbox", True, "صندوق اصطناعي؛ لا اتصال حقيقي")])

    @classmethod
    def reset(cls) -> None:
        cls.outbox = []
        cls.inbox = []
        cls.fail_mode = None
