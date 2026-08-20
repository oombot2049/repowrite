from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from nexapilot.config import OpenAIConfig, ProviderPricingConfig
from nexapilot.llm.errors import (
    ProviderCallFailed,
    ProviderError,
    ProviderErrorCategory,
)

Transport = Literal["chat_completions", "responses"]


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    profile: str
    profile_version: str
    transports: tuple[Transport, ...]
    preferred_transport: Transport
    tools_by_transport: dict[Transport, bool]
    reasoning_efforts_by_transport: dict[Transport, tuple[str, ...]]
    provider_state_by_transport: dict[Transport, bool]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderRequestPlan:
    provider: str
    model: str
    transport: Transport
    capability_profile: str
    capability_profile_version: str
    reasoning_effort: str
    fallback: bool = False
    fallback_reason: str | None = None
    provider_id: str | None = None
    model_alias: str | None = None
    endpoint_hash: str | None = None
    adapter_key: str | None = None
    pricing: ProviderPricingConfig | None = None
    route_name: str | None = None
    fallback_categories: tuple[str, ...] = ()
    max_total_attempts: int | None = None


_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")


def _configuration_error(message: str) -> ProviderCallFailed:
    return ProviderCallFailed(
        ProviderError(
            code="provider_capability_mismatch",
            category=ProviderErrorCategory.CONFIGURATION,
            retryable=False,
            safe_to_retry_before_output=False,
            public_message=message,
            diagnostic_summary=message,
        )
    )


class CapabilityResolver:
    def __init__(self, config: OpenAIConfig) -> None:
        self._config = config

    def resolve(self, *, model: str | None = None) -> ModelCapabilities:
        model_id = model or self._config.model
        requested_profile = self._config.capability_profile
        host = (urlparse(self._config.base_url).hostname or "").lower()
        profile = requested_profile
        if profile == "auto":
            profile = "openai" if host == "api.openai.com" else "openai_compatible"

        if profile == "openai":
            return ModelCapabilities(
                provider="openai",
                model=model_id,
                profile="openai",
                profile_version="2026-08-19",
                transports=("responses", "chat_completions"),
                preferred_transport="responses",
                tools_by_transport={"responses": True, "chat_completions": True},
                reasoning_efforts_by_transport={
                    "responses": _REASONING_LEVELS,
                    # A conservative common denominator. Model-specific profiles
                    # can widen this after contract tests prove compatibility.
                    "chat_completions": ("none",),
                },
                provider_state_by_transport={
                    "responses": True,
                    "chat_completions": False,
                },
                source="builtin",
            )

        # OpenAI-compatible endpoints vary widely. Preserve explicit transports,
        # while auto uses a conservative preference based on requested features.
        return ModelCapabilities(
            provider="openai-compatible",
            model=model_id,
            profile="openai_compatible",
            profile_version="2026-08-19",
            transports=("chat_completions", "responses"),
            preferred_transport="chat_completions",
            tools_by_transport={"responses": True, "chat_completions": True},
            reasoning_efforts_by_transport={
                "responses": _REASONING_LEVELS,
                "chat_completions": ("none",),
            },
            provider_state_by_transport={
                "responses": True,
                "chat_completions": False,
            },
            source="builtin-conservative",
        )

    def plans(
        self,
        *,
        tools: list[dict[str, Any]],
        params: dict[str, object] | None,
        model: str | None = None,
    ) -> list[ProviderRequestPlan]:
        capabilities = self.resolve(model=model)
        options = (params or {}).get("options")
        option_map = options if isinstance(options, dict) else {}
        option_reasoning = option_map.get("reasoning_effort")
        explicit_reasoning = (
            str(option_reasoning).strip().lower()
            if option_reasoning is not None
            else None
        )
        configured_transport = self._config.transport
        if explicit_reasoning is not None:
            reasoning_effort = explicit_reasoning
        elif configured_transport in ("auto", "responses"):
            reasoning_effort = self._config.reasoning_effort
        else:
            reasoning_effort = "none"
        if reasoning_effort not in _REASONING_LEVELS:
            raise _configuration_error(
                f"Unsupported reasoning effort: {reasoning_effort}."
            )

        if configured_transport == "auto":
            preferred: Transport
            if reasoning_effort != "none":
                preferred = "responses"
            else:
                preferred = capabilities.preferred_transport
            candidates = [preferred]
            other: Transport = (
                "chat_completions" if preferred == "responses" else "responses"
            )
            if self._config.resilience.fallback.same_model_transport:
                candidates.append(other)
        else:
            candidates = [configured_transport]

        plans: list[ProviderRequestPlan] = []
        rejected: list[str] = []
        for transport in candidates:
            if transport not in capabilities.transports:
                rejected.append(f"{transport}: transport unsupported")
                continue
            if tools and not capabilities.tools_by_transport.get(transport, False):
                rejected.append(f"{transport}: tools unsupported")
                continue
            supported_reasoning = capabilities.reasoning_efforts_by_transport.get(
                transport, ("none",)
            )
            if reasoning_effort not in supported_reasoning:
                rejected.append(
                    f"{transport}: reasoning_effort={reasoning_effort} unsupported"
                )
                continue
            plans.append(
                ProviderRequestPlan(
                    provider=capabilities.provider,
                    model=model or self._config.model,
                    transport=transport,
                    capability_profile=capabilities.profile,
                    capability_profile_version=capabilities.profile_version,
                    reasoning_effort=reasoning_effort,
                    fallback=bool(plans),
                )
            )

        if not plans:
            detail = "; ".join(rejected) or "no compatible transport"
            raise _configuration_error(
                "The selected model, transport, tools, and reasoning settings are "
                f"not compatible ({detail})."
            )
        return plans
