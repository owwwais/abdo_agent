"""بناء عملاء القنوات (البريد، البحث، تيليجرام) من الإعدادات والأسرار. بيئة demo تفرض الاصطناعي.

TEST_HOOKS يسمح للاختبارات بحقن نقل شبكي مزيف دون المساس بمسار الإنتاج.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.mail import FakeMailbox, Mailbox, SmtpImapMailbox, SmtpImapSettings
from app.connectors.search import BraveSearchClient, FakeSearchClient, SearchClient
from app.connectors.telegram import TelegramClient
from app.services import integrations as integ
from app.services.secrets import get_secret

TEST_HOOKS: dict[str, Any] = {"mailbox": None, "telegram_transport": None, "search_transport": None}


class ChannelNotReady(Exception):
    def __init__(self, message: str, code: str = "not_configured") -> None:
        self.code = code
        super().__init__(message)


@dataclass
class MailContext:
    config: integ.MailConfig
    mailbox: Mailbox
    real: bool


async def mail_context(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> MailContext:
    cfg = await integ.get_config(db, settings, workspace_id, "mail", integ.MailConfig)
    if TEST_HOOKS["mailbox"] is not None:
        return MailContext(cfg, TEST_HOOKS["mailbox"], cfg.provider == "smtp")
    if integ.forced_fake(settings) or cfg.provider == "fake":
        return MailContext(cfg, FakeMailbox(), False)
    smtp_password, _ = await get_secret(db, settings, workspace_id, "smtp_password")
    if not (cfg.configured and smtp_password):
        raise ChannelNotReady("البريد غير مهيأ: أكمل إعدادات SMTP وكلمة المرور")
    imap_password = smtp_password
    if not cfg.imap_same_password:
        imap_password, _ = await get_secret(db, settings, workspace_id, "imap_password")
    box = SmtpImapMailbox(
        SmtpImapSettings(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_security=cfg.smtp_security,
            smtp_username=cfg.smtp_username,
            smtp_password=smtp_password,
            imap_enabled=cfg.imap_enabled,
            imap_host=cfg.imap_host,
            imap_port=cfg.imap_port,
            imap_username=cfg.imap_username,
            imap_password=imap_password,
            imap_folder=cfg.imap_folder,
            sent_folder=cfg.sent_folder,
        )
    )
    return MailContext(cfg, box, True)


@dataclass
class SearchContext:
    config: integ.SearchConfig
    client: SearchClient
    price_per_request: Decimal


async def search_context(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> SearchContext:
    cfg = await integ.get_config(db, settings, workspace_id, "search", integ.SearchConfig)
    if integ.forced_fake(settings) or cfg.provider == "fake":
        return SearchContext(cfg, FakeSearchClient(), Decimal("0"))
    key, _ = await get_secret(db, settings, workspace_id, "brave_api_key")
    if not key:
        raise ChannelNotReady("مفتاح Brave Search غير مضبوط")
    transport: httpx.AsyncBaseTransport | None = TEST_HOOKS["search_transport"]
    price = (cfg.price_per_1k_requests or Decimal("0")) / Decimal(1000)
    return SearchContext(
        cfg,
        BraveSearchClient(key, country=cfg.country, lang=cfg.search_lang, transport=transport),
        price,
    )


@dataclass
class TelegramContext:
    config: integ.TelegramConfig
    client: TelegramClient | None


async def telegram_context(
    db: AsyncSession, settings: Settings, workspace_id: uuid.UUID
) -> TelegramContext:
    cfg = await integ.get_config(db, settings, workspace_id, "telegram", integ.TelegramConfig)
    token, _ = await get_secret(db, settings, workspace_id, "telegram_bot_token")
    if not (cfg.enabled and token):
        return TelegramContext(cfg, None)
    return TelegramContext(cfg, TelegramClient(token, transport=TEST_HOOKS["telegram_transport"]))
