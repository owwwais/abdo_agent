"""إعدادات التطبيق من متغيرات البيئة، مع حواجز تمنع تشغيل إعدادات التطوير في بيئات حقيقية."""

from __future__ import annotations

import logging
from decimal import Decimal
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

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_allowed_chat_id: str = ""

    search_provider: str = "fake"
    search_api_key: SecretStr = SecretStr("")
    model_provider: str = "fake"
    model_extractor_id: str = ""
    model_writer_id: str = ""
    model_api_key: SecretStr = SecretStr("")
    mail_provider: str = "fake"
    mail_credential_ref: str = ""
    outbound_enabled: bool = False

    # جلب صفحات الويب العامة لاختبار المصادر. معطل افتراضيًا في demo/test.
    source_fetch_enabled: bool | None = None
    fetch_user_agent: str = "SalesResearchAgent/0.1 (source-check; contact via app owner)"
    fetch_allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])

    max_qualified_per_day: int = 3
    daily_budget_amount: Decimal | None = None
    monthly_budget_amount: Decimal | None = None
    budget_currency: str = "USD"

    @model_validator(mode="after")
    def _apply_guards(self) -> Self:
        if self.source_fetch_enabled is None:
            self.source_fetch_enabled = self.app_env not in (AppEnv.demo, AppEnv.test)
        if self.app_env.is_local:
            if not self.session_secret.get_secret_value():
                self.session_secret = SecretStr(_DEV_SESSION_SECRET)
            if not self.data_hash_key.get_secret_value():
                self.data_hash_key = SecretStr(_DEV_DATA_HASH_KEY)
        _check_live_guards(self)
        return self

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_publishable_key)

    @property
    def cookie_secure(self) -> bool:
        return urlsplit(self.app_base_url).scheme == "https"


def _check_live_guards(s: Settings) -> None:
    problems: list[str] = []
    if s.app_env == AppEnv.demo:
        for name in ("search_provider", "model_provider", "mail_provider"):
            if getattr(s, name) != "fake":
                problems.append(f"{name.upper()} يجب أن يكون fake في بيئة demo")
        if s.outbound_enabled:
            problems.append("OUTBOUND_ENABLED ممنوع في بيئة demo")
    if not s.app_env.is_local:
        if s.dev_auth_enabled:
            problems.append("DEV_AUTH_ENABLED ممنوع خارج demo/development/test")
        secret = s.session_secret.get_secret_value()
        if len(secret) < 32 or secret == _DEV_SESSION_SECRET:
            problems.append("SESSION_SECRET مطلوب (32 حرفًا على الأقل) وليس قيمة التطوير")
        hash_key = s.data_hash_key.get_secret_value()
        if len(hash_key) < 32 or hash_key == _DEV_DATA_HASH_KEY:
            problems.append("DATA_HASH_KEY مطلوب (32 حرفًا على الأقل) وليس قيمة التطوير")
        if not s.database_url:
            problems.append("DATABASE_URL مطلوب")
        if not s.supabase_configured:
            problems.append("SUPABASE_URL وSUPABASE_PUBLISHABLE_KEY مطلوبان")
        if not s.cookie_secure:
            problems.append("APP_BASE_URL يجب أن يستخدم https")
        if s.daily_budget_amount is None or s.monthly_budget_amount is None:
            problems.append(
                "DAILY_BUDGET_AMOUNT وMONTHLY_BUDGET_AMOUNT مطلوبان خارج البيئات المحلية"
            )
    if s.app_env == AppEnv.production:
        for name in ("search_provider", "model_provider", "mail_provider"):
            if getattr(s, name) == "fake":
                problems.append(f"{name.upper()}=fake ممنوع في production")
    if problems:
        raise ConfigError("إعداد غير صالح: " + "؛ ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
