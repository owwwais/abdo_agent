"""عميل Telegram Bot API مباشر (httpx). الويب هوك يتحقق من ترويسة X-Telegram-Bot-Api-Secret-Token؛
وفي التطوير المحلي يُستخدم getUpdates (polling) لأن الويب هوك يحتاج رابط https عامًا.

callback_data في الأزرار رمز opaque قصير (≤64 بايت) يُحل من جدول telegram_callbacks على الخادم.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.telegram.org"
MAX_TEXT = 4000


class TelegramError(Exception):
    def __init__(
        self, message: str, *, code: str = "telegram_error", retryable: bool = False
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = f"{API}/bot{token}"
        self._transport = transport

    async def call(
        self, method: str, payload: dict[str, Any] | None = None, *, http_timeout: float = 20.0
    ) -> Any:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=http_timeout) as client:
                resp = await client.post(f"{self._base}/{method}", json=payload or {})
        except httpx.HTTPError as exc:
            raise TelegramError("تعذر الاتصال بتيليجرام", retryable=True, code="network") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"رد غير صالح من تيليجرام ({resp.status_code})") from exc
        if not data.get("ok"):
            desc = str(data.get("description", ""))[:200]
            code = "auth" if resp.status_code == 401 else "api_error"
            raise TelegramError(
                f"تيليجرام رفض الطلب: {desc}", code=code, retryable=resp.status_code == 429
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.call("getMe")
        return result

    async def send_message(
        self, chat_id: str | int, text: str, *, buttons: list[list[dict[str, str]]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        result: dict[str, Any] = await self.call("sendMessage", payload)
        return result

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, buttons: list[list[dict[str, str]]] | None
    ) -> None:
        await self.call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": buttons or []},
            },
        )

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call(
            "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]}
        )

    async def set_webhook(self, url: str, secret: str) -> None:
        await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, offset: int, *, long_poll: int = 0) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": long_poll,
                "allowed_updates": ["message", "callback_query"],
            },
            http_timeout=long_poll + 15.0,
        )
        return result
