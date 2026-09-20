from pathlib import Path

import pytest
from pydantic import ValidationError

from algen_agent_runtime.config.settings import (
    ApiSettings,
    AppSettings,
    ProviderSettings,
    load_settings,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import RunRequest


def test_literal_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="secret reference"):
        ProviderSettings(type="openai", default_model="x", api_key="literal-secret")


def test_unknown_configuration_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("unknown: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid configuration"):
        load_settings((config,))


def test_nested_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGEN_AGENT_RUNTIME__API__PORT", "9000")
    assert load_settings().api.port == 9000


def test_development_auth_defaults_to_loopback() -> None:
    settings = ApiSettings()
    assert settings.host == "127.0.0.1"
    assert settings.allow_insecure_development_auth is False


def test_development_auth_rejects_public_bind_without_explicit_override() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ApiSettings(host="0.0.0.0")
    settings = ApiSettings(host="0.0.0.0", allow_insecure_development_auth=True)
    assert settings.host == "0.0.0.0"


def test_nested_traccia_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENABLED", "true")
    monkeypatch.setenv("ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__SAMPLE_RATE", "0.25")
    settings = load_settings()
    assert settings.telemetry.traccia.enabled is True
    assert settings.telemetry.traccia.sample_rate == 0.25


def test_cache_configuration_supports_scoped_policies() -> None:
    settings = AppSettings.model_validate(
        {
            "cache": {
                "backend": "memory",
                "policies": {
                    "retrieval": {"enabled": True, "scope": "tenant", "ttl_seconds": 60},
                    "query_results": {"enabled": True, "scope": "user", "ttl_seconds": 15},
                },
            }
        }
    )
    assert settings.cache.policies["retrieval"].scope.value == "tenant"
    assert settings.cache.policies["query_results"].scope.value == "user"


def test_query_sources_require_secret_references_and_remain_separate_from_storage() -> None:
    settings = AppSettings.model_validate(
        {
            "query_sources": {
                "analytics": {
                    "type": "postgres",
                    "connection_url": "env://ANALYTICS_QUERY_DSN",
                    "purpose": "executive_analytics",
                    "authorization_tags": ["analytics.read"],
                }
            }
        }
    )
    assert settings.query_sources["analytics"].read_only is True
    assert settings.query_sources["analytics"].connection_url == "env://ANALYTICS_QUERY_DSN"
    assert settings.storage.postgres_dsn is None
    with pytest.raises(ValidationError, match="env://"):
        AppSettings.model_validate(
            {
                "query_sources": {
                    "analytics": {
                        "connection_url": "postgresql://user:secret@localhost/db",
                        "purpose": "analytics",
                    }
                }
            }
        )


def test_redis_cache_requires_environment_secret_reference() -> None:
    with pytest.raises(ValidationError, match="requires redis_url"):
        AppSettings.model_validate({"cache": {"backend": "redis"}})
    with pytest.raises(ValidationError, match="env://"):
        AppSettings.model_validate(
            {"cache": {"backend": "redis", "redis_url": "redis://localhost:6379"}}
        )


def test_telemetry_content_capture_is_bounded_and_disabled_by_default() -> None:
    settings = AppSettings()
    assert settings.telemetry.include_content is False
    assert settings.telemetry.max_content_chars == 16_384
    with pytest.raises(ValidationError):
        AppSettings.model_validate(
            {"telemetry": {"include_content": True, "max_content_chars": 100}}
        )


def test_conversation_presentation_defaults_to_business_safe_output() -> None:
    settings = AppSettings()
    assert settings.conversation_presentation.progress_audience == "business"
    assert settings.conversation_presentation.error_audience == "business"
    assert settings.conversation_presentation.show_technical_details is False
    assert settings.conversation_presentation.technical_details_expanded is False


def test_unknown_request_override_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RunRequest(
            agent="agent",
            input="x",
            tenant_id="tenant",
            user_id="user",
            overrides={"unbounded_option": True},
        )


def test_mistral_provider_configuration_is_valid() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {
                "mistral": {
                    "type": "mistral",
                    "api_key": "env://MISTRAL_API_KEY",
                    "default_model": "mistral-small-latest",
                }
            }
        }
    )
    assert settings.providers["mistral"].api_key == "env://MISTRAL_API_KEY"
    providers = build_container(settings).runtime.router.providers()
    assert tuple(provider.provider_id for provider in providers) == ("mistral",)
