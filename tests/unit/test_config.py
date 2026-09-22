from __future__ import annotations

from typing import Any

import pytest

from app.config import ConfigError, Settings

SECRET = "x" * 40


def build(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def live_ok(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        app_env="staging",
        database_url="postgresql://db/x",
        supabase_url="https://p.supabase.co",
        supabase_publishable_key="sb_publishable_x",
        session_secret=SECRET,
        data_hash_key=SECRET + "y",
        app_base_url="https://app.example.com",
        daily_budget_amount="5",
        monthly_budget_amount="50",
    )
    base.update(overrides)
    return base


def test_staging_with_required_values_starts() -> None:
    s = build(**live_ok())
    assert s.cookie_secure and not s.dev_auth_enabled


def test_dev_auth_refused_outside_local_envs() -> None:
    with pytest.raises(ConfigError, match="DEV_AUTH_ENABLED"):
        build(**live_ok(dev_auth_enabled=True))
    with pytest.raises(ConfigError, match="DEV_AUTH_ENABLED"):
        build(
            **live_ok(
                app_env="production",
                dev_auth_enabled=True,
                search_provider="x",
                model_provider="x",
                mail_provider="x",
            )
        )


def test_live_env_requires_secrets_budget_and_https() -> None:
    with pytest.raises(ConfigError) as err:
        build(
            app_env="production", database_url="postgresql://db/x", app_base_url="http://insecure"
        )
    msg = str(err.value)
    for needle in (
        "SESSION_SECRET",
        "DATA_HASH_KEY",
        "SUPABASE_URL",
        "https",
        "DAILY_BUDGET_AMOUNT",
        "=fake",
    ):
        assert needle in msg
    assert SECRET not in msg


def test_demo_forbids_real_providers_and_outbound() -> None:
    with pytest.raises(ConfigError, match="MODEL_PROVIDER"):
        build(app_env="demo", model_provider="openai")
    with pytest.raises(ConfigError, match="OUTBOUND_ENABLED"):
        build(app_env="demo", outbound_enabled=True)


def test_local_envs_get_dev_only_secrets_and_fetch_defaults() -> None:
    demo = build(app_env="demo")
    assert demo.session_secret.get_secret_value().startswith("dev-only")
    assert demo.source_fetch_enabled is False
    assert build(app_env="development").source_fetch_enabled is True


def test_dev_secret_values_rejected_in_live() -> None:
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        build(**live_ok(session_secret="dev-only-session-secret-not-for-real-use-000000"))
