from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from nexapilot.config import ModelGatewayConfig, OpenAIConfig
from nexapilot.llm.capabilities import CapabilityResolver, ProviderRequestPlan
from nexapilot.llm.errors import ProviderCallFailed


@dataclass(frozen=True)
class RejectedRouteCandidate:
    model_alias: str
    reason: str


@dataclass(frozen=True)
class ModelRoutePlan:
    route_name: str
    candidates: tuple[ProviderRequestPlan, ...]
    rejected: tuple[RejectedRouteCandidate, ...]
    max_total_attempts: int


_CATEGORY_VALUES = {
    "connection": "connection",
    "timeout": "timeout",
    "rate_limit": "rate_limited",
    "server": "server_error",
    "circuit_open": "circuit_open",
    "model_unavailable": "not_found",
}


def _endpoint_hash(base_url: str) -> str:
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:20]


def _estimated_input_tokens(system: str, messages: list[dict[str, Any]]) -> int:
    # This is deliberately conservative and tokenizer-independent. Provider
    # token usage remains the source of truth after execution.
    chars = len(system) + len(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return max(1, (chars + 2) // 3)


class ModelRouter:
    def __init__(self, config: ModelGatewayConfig, legacy: OpenAIConfig) -> None:
        self._config = config
        self._legacy = legacy

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def plan(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, object] | None,
        requested_model: str | None,
        route_name: str | None = None,
    ) -> ModelRoutePlan:
        selected_route = route_name or self._config.default_route
        route = self._config.routes.get(selected_route)
        if route is None:
            raise RuntimeError(f"unknown model route: {selected_route}")

        aliases = list(route.candidates)
        if requested_model in self._config.models:
            requested_index = aliases.index(requested_model) if requested_model in aliases else -1
            if requested_index >= 0:
                aliases = aliases[requested_index:]
            else:
                aliases.insert(0, str(requested_model))
        aliases = aliases[: route.max_fallback_hops + 1]

        option_map: dict[str, Any] = {}
        if isinstance(params, dict) and isinstance(params.get("options"), dict):
            option_map = params["options"]  # type: ignore[assignment]
        reasoning_effort = str(
            option_map.get("reasoning_effort") or self._legacy.reasoning_effort
        ).lower()
        structured_output_required = bool(
            isinstance(params, dict)
            and (params.get("response_format") or option_map.get("response_format"))
        )
        provider_state_required = any(
            isinstance(message.get("provider_state"), list)
            and bool(message.get("provider_state"))
            for message in messages
        )
        estimated_tokens = _estimated_input_tokens(system, messages)

        plans: list[ProviderRequestPlan] = []
        rejected: list[RejectedRouteCandidate] = []
        first_provider: str | None = None
        fallback_categories = tuple(
            _CATEGORY_VALUES[item] for item in route.fallback_on
        )

        for alias in aliases:
            target = self._config.models[alias]
            provider = self._config.providers[target.provider]
            if first_provider is None:
                first_provider = target.provider
            elif not route.allow_cross_provider and target.provider != first_provider:
                rejected.append(
                    RejectedRouteCandidate(alias, "cross_provider_disabled")
                )
                continue
            if tools and not target.tools:
                rejected.append(RejectedRouteCandidate(alias, "tools_unsupported"))
                continue
            if reasoning_effort not in target.reasoning_efforts:
                rejected.append(
                    RejectedRouteCandidate(
                        alias, f"reasoning_effort_{reasoning_effort}_unsupported"
                    )
                )
                continue
            if provider_state_required and not target.provider_state:
                rejected.append(
                    RejectedRouteCandidate(alias, "provider_state_unsupported")
                )
                continue
            if (
                provider_state_required
                and first_provider is not None
                and target.provider != first_provider
            ):
                # Opaque provider state (for example a Responses reasoning item)
                # is not portable across provider boundaries. Falling back here
                # would either be rejected or, worse, silently lose continuity.
                rejected.append(
                    RejectedRouteCandidate(alias, "provider_state_not_portable")
                )
                continue
            if structured_output_required and not target.structured_output:
                rejected.append(
                    RejectedRouteCandidate(alias, "structured_output_unsupported")
                )
                continue
            if target.context_window and estimated_tokens > target.context_window:
                rejected.append(
                    RejectedRouteCandidate(
                        alias,
                        f"context_window_insufficient:{estimated_tokens}>{target.context_window}",
                    )
                )
                continue

            target_config = replace(
                self._legacy,
                base_url=provider.base_url,
                api_key="configured-by-environment",
                model=target.model,
                transport=target.transport,
                capability_profile=provider.capability_profile,
                pricing=target.pricing,
            )
            try:
                target_plans = CapabilityResolver(target_config).plans(
                    tools=tools,
                    params=params,
                    model=target.model,
                )
            except ProviderCallFailed as exc:
                rejected.append(RejectedRouteCandidate(alias, exc.error.code))
                continue
            target_plans = [
                plan for plan in target_plans if plan.transport in provider.transports
            ]
            if not target_plans:
                rejected.append(
                    RejectedRouteCandidate(alias, "provider_transport_unsupported")
                )
                continue
            for index, plan in enumerate(target_plans):
                if not plans:
                    fallback_reason = None
                elif index > 0:
                    fallback_reason = "transport_fallback"
                else:
                    fallback_reason = "model_fallback"
                plans.append(
                    replace(
                        plan,
                        provider=target.provider,
                        fallback=bool(plans),
                        fallback_reason=fallback_reason,
                        provider_id=target.provider,
                        model_alias=alias,
                        endpoint_hash=_endpoint_hash(provider.base_url),
                        adapter_key=f"{target.provider}:{plan.transport}",
                        pricing=target.pricing,
                        route_name=selected_route,
                        fallback_categories=fallback_categories,
                        max_total_attempts=route.max_total_attempts,
                    )
                )
        if not plans:
            detail = "; ".join(
                f"{item.model_alias}: {item.reason}" for item in rejected
            )
            raise RuntimeError(f"model route produced no compatible target ({detail})")
        return ModelRoutePlan(
            route_name=selected_route,
            candidates=tuple(plans),
            rejected=tuple(rejected),
            max_total_attempts=route.max_total_attempts,
        )
