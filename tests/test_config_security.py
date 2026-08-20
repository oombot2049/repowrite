from __future__ import annotations

from nexapilot.config import (
    REDACTED_CONFIG_VALUE,
    mask_sensitive,
    redact_sensitive,
    restore_redacted_sensitive,
)


def _config() -> dict:
    return {
        "openai": {"api_key": "sk-secret", "model": "test-model"},
        "langfuse": {"secret_key": "lf-secret", "enabled": True},
        "web_search": {"tavily_api_key": ""},
    }


def test_public_config_exposes_only_secret_configuration_state() -> None:
    public = mask_sensitive(_config())
    assert public["openai"]["api_key"] == {"configured": True}
    assert public["langfuse"]["secret_key"] == {"configured": True}
    assert public["web_search"]["tavily_api_key"] == {"configured": False}
    assert public["openai"]["model"] == "test-model"
    assert "sk-secret" not in repr(public)


def test_raw_config_redaction_reports_configured_fields() -> None:
    redacted, configured = redact_sensitive(_config())
    assert redacted["openai"]["api_key"] == REDACTED_CONFIG_VALUE
    assert redacted["langfuse"]["secret_key"] == REDACTED_CONFIG_VALUE
    assert redacted["web_search"]["tavily_api_key"] == ""
    assert configured == ["openai.api_key", "langfuse.secret_key"]


def test_saving_redacted_raw_config_preserves_existing_secrets() -> None:
    redacted, _ = redact_sensitive(_config())
    redacted["openai"]["model"] = "new-model"
    restored = restore_redacted_sensitive(redacted, _config())
    assert restored["openai"]["api_key"] == "sk-secret"
    assert restored["langfuse"]["secret_key"] == "lf-secret"
    assert restored["openai"]["model"] == "new-model"


def test_unknown_redacted_placeholder_is_not_written_as_a_secret() -> None:
    incoming = {"openai": {"api_key": REDACTED_CONFIG_VALUE, "model": "m"}}
    restored = restore_redacted_sensitive(incoming, {"openai": {"model": "m"}})
    assert "api_key" not in restored["openai"]
