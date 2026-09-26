"""Hostinger Mail API: شكل الطلبات كما في مواصفة OpenAPI 1.1.0، ودلالة التسليم لكل حالة."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.connectors.hostinger_mail import API_BASE, HostingerApiMailbox
from app.connectors.mail import OutgoingMail

ME = {
    "data": {
        "orderResourceId": "OR1",
        "mailboxes": [{"resourceId": "AC123", "address": "sales@acme.example"}],
    }
}
SENT = {
    "data": [
        {
            "uid": 9,
            "subject": "موضوع آخر",
            "to": [{"name": "", "address": "buyer@clinic.example"}],
            "messageId": "<old@hostinger>",
        },
        {
            "uid": 10,
            "subject": "تنظيم المواعيد",
            "to": [{"name": "", "address": "buyer@clinic.example"}],
            "messageId": "<new-123@hostinger>",
        },
    ],
    "pagination": {},
}


def mail(**kw: Any) -> OutgoingMail:
    base = dict(
        from_address="sales@acme.example",
        from_name="فريق أكمي",
        to_address="buyer@clinic.example",
        subject="تنظيم المواعيد",
        body="السلام عليكم",
    )
    base.update(kw)
    return OutgoingMail(**base)  # type: ignore[arg-type]


def box(
    send: Callable[[httpx.Request], httpx.Response],
) -> tuple[HostingerApiMailbox, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path.endswith("/me"):
            return httpx.Response(200, json=ME)
        if path.endswith("/send"):
            return send(req)
        if "/folders/" in path:
            return httpx.Response(200, json=SENT)
        return httpx.Response(404)

    return (
        HostingerApiMailbox(
            "tok-test", "Sales@Acme.example", imap=None, transport=httpx.MockTransport(handler)
        ),
        seen,
    )


async def test_send_uses_bearer_resolves_mailbox_and_reads_message_id() -> None:
    mb, seen = box(lambda r: httpx.Response(204))
    out = await mb.send(mail(reply_uid="42", reply_folder="INBOX"))
    assert out.status == "sent" and out.sent_copy_saved
    assert out.message_id == "<new-123@hostinger>"  # من نسخة المرسل المطابقة، لا الأحدث عشوائيًا
    send = next(r for r in seen if r.url.path.endswith("/send"))
    assert str(send.url) == f"{API_BASE}/mailboxes/AC123/send"
    assert send.headers["Authorization"] == "Bearer tok-test"
    body = json.loads(send.content)
    assert body == {
        "to": ["buyer@clinic.example"],
        "subject": "تنظيم المواعيد",
        "text": "السلام عليكم",
        "displayName": "فريق أكمي",
        "inReplyTo": {"uid": 42, "folder": "INBOX"},
    }
    listing = next(r for r in seen if "/folders/" in r.url.path)
    assert "INBOX.Sent" in listing.url.path and listing.url.params["sort"] == "-uid"


async def test_connect_error_is_safe_to_retry() -> None:
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=r)

    mb, _ = box(refuse)
    out = await mb.send(mail())
    assert out.status == "failed" and out.retryable


@pytest.mark.parametrize(
    "respond",
    [
        lambda r: httpx.Response(502, json={"error": "Bad gateway", "code": "ERR_BAD_GATEWAY"}),
        lambda r: httpx.Response(504),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
    ],
)
async def test_ambiguous_outcomes_are_unknown_delivery(
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    mb, _ = box(respond)
    out = await mb.send(mail())
    assert out.status == "unknown_delivery" and not out.retryable


async def test_validation_error_is_final_and_explained() -> None:
    mb, _ = box(
        lambda r: httpx.Response(
            422,
            json={
                "error": "The given data was invalid.",
                "code": "ERR_VALIDATION_FAILED",
                "params": {"to": ["invalid"]},
            },
        )
    )
    out = await mb.send(mail())
    assert out.status == "failed" and not out.retryable and "422" in out.detail


async def test_token_without_this_mailbox_fails_clearly_and_test_reports_it() -> None:
    mb = HostingerApiMailbox(
        "tok",
        "other@acme.example",
        imap=None,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ME)),
    )
    out = await mb.send(mail())
    assert out.status == "failed" and "sales@acme.example" in out.detail
    report = await mb.test()
    assert not report.ok
    assert any(
        name == "Hostinger API" and not ok and "لا يدير" in msg for name, ok, msg in report.items
    )
    assert any(name == "IMAP" and not ok for name, ok, _ in report.items)


async def test_invalid_token_is_reported() -> None:
    mb = HostingerApiMailbox(
        "bad",
        "sales@acme.example",
        imap=None,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                401, json={"error": "Missing or invalid credentials.", "code": "ERR_UNAUTHORIZED"}
            )
        ),
    )
    report = await mb.test()
    assert ("Hostinger API", False, "الرمز غير صالح أو منتهي") in report.items
