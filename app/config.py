"""إعدادات التطبيق من متغيرات البيئة، مع حواجز تمنع تشغيل إعدادات التطوير في بيئات حقيقية."""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# قيم تطوير فقط؛ ممنوعة خارج demo/development/test (انظر _check_live_guards).
_DEV_SESSION_SECRET = "dev-only-session-secret-not-for-real-use-000000"  # noqa: S105
_DEV_DATA_HASH_KEY = "dev-only-data-hash-key-not-for-real-use-00000000"
# مفتاح Fernet ثابت للتطوير المحلي فقط (base64 لـ32 بايت)؛ مرفوض خارج البيئات المحلية.
_DEV_FERNET_KEY = "ZGV2LW9ubHktZmVybmV0LWtleS1ub3QtZm9yLXJlYWw="


class AppEnv(StrEnum):
    demo = "demo"
    development = "development"
    test = "test"
    staging = "staging"
    production = "production"

    @property
    def is_local(self) -> bool:
        return self in (AppEnv.demo, AppEnv.development, AppEnv.test)


class ConfigError(RuntimeError):
    """إعداد غير صالح يمنع الإقلاع. الرسالة لا تحتوي قيم أسرار."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    app_env: AppEnv = AppEnv.demo
    app_timezone: str = "Asia/Riyadh"
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    database_url: str = ""
    checkpoint_database_url: str = ""
    db_pool_size: int = 5

    # Supabase Auth: المفتاح المنشور آمن للكشف؛ التحقق من JWT عبر JWKS العام للمشروع.
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_jwt_audience: str = "authenticated"

    session_secret: SecretStr = SecretStr("")
    session_idle_hours: int = 12
    session_absolute_days: int = 7
    # مفتاح HMAC ثابت لبصمات منع التواصل وجهات الاتصال. تغييره يكسر مطابقة السجل القديم.
    data_hash_key: SecretStr = SecretStr("")

    dev_auth_enabled: bool = False

    # مفتاح/مفاتيح Fernet لتشفير الأسرار المحفوظة من صفحة الإعدادات (مفصولة بفواصل؛ الأول للتشفير
    # والباقي لفك تشفير القديم أثناء التدوير). مطلوب خارج البيئات المحلية.
    secrets_encryption_key: SecretStr = SecretStr("")

    # مفتاح التحقق من ويب هوك بريد Hostinger (Agentic Mail → Webhooks). يُرسل كـ Bearer.
    hostinger_webhook_secret: SecretStr = SecretStr("")

    # قيم احتياطية اختيارية للتكاملات. ما يُحفظ من صفحة الإعدادات يتقدم عليها.
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    openai_compatible_api_key: SecretStr = SecretStr("")
    openai_compatible_base_url: str = ""
    brave_search_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int | None = None
    smtp_security: str = ""
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    imap_host: str = ""
    imap_port: int | None = None
    imap_username: str = ""
    imap_password: SecretStr = SecretStr("")
    mail_from_address: str = ""
    mail_from_name: str = ""

    # مفتاح إيقاف عام لكل إرسال خارجي. لا يُرسل أي بريد حقيقي إن لم يكن true، حتى بعد الاعتماد.
    outbound_enabled: bool = False
    # قائمة سماح اختيارية للمستلمين (بريد أو @نطاق) مفصولة بفواصل؛ مفيدة في staging.
    outbound_allowlist: str = ""

    # جلب صفحات الويب العامة لاختبار المصادر والإثراء. معطل افتراضيًا في demo/test.
    source_fetch_enabled: bool | None = None
    fetch_user_agent: str = "SalesResearchAgent/0.1 (source-check; contact via app owner)"
    fetch_allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])

    @model_validator(mode="after")
    def _apply_guards(self) -> Self:
        if self.source_fetch_enabled is None:
            self.source_fetch_enabled = self.app_env not in (AppEnv.demo, AppEnv.test)
        if self.app_env.is_local:
            if not self.session_secret.get_secret_value():
                self.session_secret = SecretStr(_DEV_SESSION_SECRET)
            if not self.data_hash_key.get_secret_value():
                self.data_hash_key = SecretStr(_DEV_DATA_HASH_KEY)
            if not self.secrets_encryption_key.get_secret_value():
                self.secrets_encryption_key = SecretStr(_DEV_FERNET_KEY)
        _check_live_guards(self)
        return self

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_publishable_key)

    @property
    def cookie_secure(self) -> bool:
        return urlsplit(self.app_base_url).scheme == "https"


def _valid_fernet_keys(raw: str) -> bool:
    from cryptography.fernet import Fernet

    try:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        for k in keys:
            Fernet(k.encode())
        return bool(keys)
    except (ValueError, TypeError):
        return False


def _check_live_guards(s: Settings) -> None:
    problems: list[str] = []
    if s.app_env == AppEnv.demo and s.outbound_enabled:
        problems.append("OUTBOUND_ENABLED ممنوع في بيئة demo")
    if not _valid_fernet_keys(s.secrets_encryption_key.get_secret_value()):
        problems.append(
            "SECRETS_ENCRYPTION_KEY ليس مفتاح Fernet صالحًا (ولّده بـ scripts/gen_secrets.py)"
        )
    if not s.app_env.is_local:
        if s.dev_auth_enabled:
            problems.append("DEV_AUTH_ENABLED ممنوع خارج demo/development/test")
        secret = s.session_secret.get_secret_value()
        if len(secret) < 32 or secret == _DEV_SESSION_SECRET:
            problems.append("SESSION_SECRET مطلوب (32 حرفًا على الأقل) وليس قيمة التطوير")
        hash_key = s.data_hash_key.get_secret_value()
        if len(hash_key) < 32 or hash_key == _DEV_DATA_HASH_KEY:
            problems.append("DATA_HASH_KEY مطلوب (32 حرفًا على الأقل) وليس قيمة التطوير")
        if _DEV_FERNET_KEY in s.secrets_encryption_key.get_secret_value():
            problems.append("SECRETS_ENCRYPTION_KEY لا يجوز أن يكون مفتاح التطوير")
        if not s.database_url:
            problems.append("DATABASE_URL مطلوب")
        if not s.supabase_configured:
            problems.append("SUPABASE_URL وSUPABASE_PUBLISHABLE_KEY مطلوبان")
        if not s.cookie_secure:
            problems.append("APP_BASE_URL يجب أن يستخدم https")
    if problems:
        raise ConfigError("إعداد غير صالح: " + "؛ ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
