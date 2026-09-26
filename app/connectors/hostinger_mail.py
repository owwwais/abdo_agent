"""إرسال عبر Hostinger Mail API (HTTPS) مع قراءة الردود عبر IMAP كما هي.

لماذا: خطة Render المجانية تمنع منافذ SMTP الصادرة (25/465/587)، بينما HTTPS وIMAP (993) مسموحان.

الواجهة (Hostinger Mail API 1.1.0، https://api.mail.hostinger.com):
- المصادقة: Authorization: Bearer <token> (Agentic mail ← API ← Create API token).
- GET /api/v1/me يعيد الصناديق التي يديرها الرمز: [{resourceId, address}].
- POST /api/v1/mailboxes/{resourceId}/send بجسم {to[], displayName, subject, text, inReplyTo?:{uid, folder}}؛
  204 = أُرسلت وحُفظت نسخة في INBOX.Sent. لا يعيد Message-ID، فنقرؤه من النسخة المحفوظة
  (GET .../folders/{folder}/messages?sort=-uid) كي تُربط ردود العميل بالمحادثة بدقة.

دلالة التسليم كما في SMTP (SPEC §8.4):
- فشل الاتصال قبل إرسال الطلب (ConnectError) = لم تُرسل؛ آمن للإعادة.
- انقطاع بعد إرسال الطلب، أو 500/502/504 = «تسليم غير معروف»؛ لا إعادة آلية.
- 401/403/422 = خطأ إعداد أو محتوى؛ لا إعادة حتى يُصلح.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.mail import InboundMail, OutgoingMail, SendOutcome, SmtpImapMailbox, TestReport

API_BASE = "https://api.mail.hostinger.com/api/v1"
DEFAULT_SENT_FOLDER = "INBOX.Sent"


class HostingerApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class HostingerMailApi:
    def __init__(
        self, token: str, *, transport: httpx.AsyncBaseTransport | None = None, base: str = API_BASE
    ) -> None:
        self._token = token
        self._transport = transport
        self._base = base.rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )

    @staticmethod
    def _error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            detail = body.get("error") or body.get("code") or ""
            params = body.get("params")
            if params:
                detail += f" {params}"
        except ValueError:
            detail = resp.text[:200]
        return f"HTTP {resp.status_code}: {detail}".strip()

    async def mailboxes(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            resp = await client.get(f"{self._base}/me")
        if resp.status_code != 200:
            raise HostingerApiError(self._error(resp), status=resp.status_code)
        data = resp.json().get("data", {})
        return list(data.get("mailboxes", []))

    async def mailbox_id(self, address: str) -> str:
        boxes = await self.mailboxes()
        for box in boxes:
            if str(box.get("address", "")).lower() == address.lower():
                return str(box["resourceId"])
        managed = ", ".join(str(b.get("address")) for b in boxes) or "لا شيء"
        raise HostingerApiError(
            f"الرمز لا يدير الصندوق {address}؛ الصناديق المتاحة للرمز: {managed}", status=403
        )

    async def recent(
        self, mailbox_id: str, folder: str, per_page: int = 10
    ) -> list[dict[str, Any]]:
        async with self._client() as client:
            resp = await client.get(
                f"{self._base}/mailboxes/{mailbox_id}/folders/{quote(folder, safe='')}/messages",
                params={"sort": "-uid", "perPage": per_page, "page": 1},
            )
        if resp.status_code != 200:
            raise HostingerApiError(self._error(resp), status=resp.status_code)
        return list(resp.json().get("data", []))


class HostingerApiMailbox:
    """الإرسال عبر Hostinger API، والقراءة عبر IMAP (إن وُجدت كلمة مرور الصندوق)."""

    name = "hostinger_api"

    def __init__(
        self,
        token: str,
        address: str,
        *,
        imap: SmtpImapMailbox | None,
        sent_folder: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api = HostingerMailApi(token, transport=transport)
        self.address = address
        self.imap = imap
        self.sent_folder = sent_folder or DEFAULT_SENT_FOLDER
        self._mailbox_id: str | None = None

    async def _resolve(self) -> str:
        if self._mailbox_id is None:
            self._mailbox_id = await self.api.mailbox_id(self.address)
        return self._mailbox_id

    async def send(self, mail: OutgoingMail) -> SendOutcome:
        try:
            mailbox_id = await self._resolve()
        except httpx.HTTPError as exc:
            return SendOutcome(
                "failed", "", f"تعذر الوصول إلى Hostinger API: {type(exc).__name__}", retryable=True
            )
        except HostingerApiError as exc:
            return SendOutcome("failed", "", str(exc), retryable=False)
        payload: dict[str, Any] = {
            "to": [mail.to_address],
            "subject": mail.subject,
            "text": mail.body,
        }
        if mail.from_name:
            payload["displayName"] = mail.from_name
        if mail.reply_uid and mail.reply_folder:
            payload["inReplyTo"] = {"uid": int(mail.reply_uid), "folder": mail.reply_folder}
        try:
            async with self.api._client() as client:
                resp = await client.post(
                    f"{self.api._base}/mailboxes/{mailbox_id}/send", json=payload
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # لم يُرسل الطلب أصلًا: آمن للإعادة.
            return SendOutcome(
                "failed", "", f"تعذر الاتصال بـHostinger API: {type(exc).__name__}", retryable=True
            )
        except httpx.HTTPError as exc:
            # الطلب خرج ولا نعرف مصيره: لا إعادة عمياء.
            return SendOutcome(
                "unknown_delivery", "", f"انقطع الاتصال بعد إرسال الطلب: {type(exc).__name__}"
            )
        if resp.status_code in (200, 201, 202, 204):
            message_id = await self._sent_message_id(mailbox_id, mail)
            return SendOutcome(
                "sent",
                message_id or "",
                "أُرسلت عبر Hostinger API"
                + ("" if message_id else " (تعذر قراءة Message-ID من المرسل)"),
                sent_copy_saved=True,
            )
        detail = HostingerMailApi._error(resp)
        if resp.status_code in (500, 502, 504):
            return SendOutcome("unknown_delivery", "", f"رد غير حاسم من Hostinger ({detail})")
        if resp.status_code == 429:
            return SendOutcome("failed", "", f"تجاوز حد الطلبات ({detail})", retryable=True)
        return SendOutcome("failed", "", f"رفض Hostinger الرسالة ({detail})", retryable=False)

    async def _sent_message_id(self, mailbox_id: str, mail: OutgoingMail) -> str | None:
        """Message-ID من نسخة المرسل الأحدث المطابقة للمستلم والموضوع. أفضل جهد: لا يُفشل الإرسال."""
        try:
            recent = await self.api.recent(mailbox_id, self.sent_folder)
        except (httpx.HTTPError, HostingerApiError):
            return None
        target = mail.to_address.lower()
        for msg in recent:
            to = [str(a.get("address", "")).lower() for a in msg.get("to", [])]
            if target in to and (msg.get("subject") or "") == mail.subject and msg.get("messageId"):
                return str(msg["messageId"])
        return None

    async def fetch_new(self, cursor: dict[str, Any]) -> tuple[list[InboundMail], dict[str, Any]]:
        if self.imap is None:
            return [], cursor
        return await self.imap.fetch_new(cursor)

    async def test(self) -> TestReport:
        report = TestReport()
        try:
            boxes = await self.api.mailboxes()
            addresses = [str(b.get("address", "")).lower() for b in boxes]
            if self.address.lower() in addresses:
                report.items.append(("Hostinger API", True, f"الرمز يعمل ويدير {self.address}"))
            else:
                report.items.append(
                    (
                        "Hostinger API",
                        False,
                        f"الرمز صالح لكنه لا يدير {self.address}؛ المتاح: {', '.join(addresses) or 'لا شيء'}",
                    )
                )
        except HostingerApiError as exc:
            msg = "الرمز غير صالح أو منتهي" if exc.status == 401 else str(exc)
            report.items.append(("Hostinger API", False, msg))
        except httpx.HTTPError as exc:
            report.items.append(("Hostinger API", False, f"تعذر الاتصال: {type(exc).__name__}"))
        if self.imap is not None:
            report.items.extend(await self.imap.test_imap())
        else:
            report.items.append(("IMAP", False, "كلمة مرور الصندوق غير مدخلة؛ الردود لن تُقرأ"))
        return report
