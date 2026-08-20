"""YAML/JSON file-based configuration with global + project-level merge.

Config discovery order (later wins):
  1. Built-in defaults (from config_schema.py)
  2. Global: ~/.nexa/config.yaml (or .json)
  3. Project: {worktree}/.nexa/config.yaml (or .json)
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from nexapilot.config_schema import CONFIG_FIELD_META

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRetryConfig:
    max_attempts: int = 3
    base_delay_ms: int = 500
    max_delay_ms: int = 8_000
    max_retry_after_ms: int = 30_000


@dataclass(frozen=True)
class ProviderTimeoutConfig:
    connect_ms: int = 10_000
    first_event_ms: int = 45_000
    idle_stream_ms: int = 30_000
    total_attempt_ms: int = 180_000


@dataclass(frozen=True)
class ProviderCircuitConfig:
    enabled: bool = True
    failure_threshold: int = 5
    failure_window_ms: int = 60_000
    cooldown_ms: int = 30_000


@dataclass(frozen=True)
class ProviderFallbackConfig:
    same_model_transport: bool = True
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderBudgetConfig:
    max_calls_per_run: int = 30
    max_attempts_per_run: int = 60
    max_input_tokens_per_run: int | None = None
    max_output_tokens_per_run: int | None = None
    max_cost_microusd_per_run: int | None = None


@dataclass(frozen=True)
class ProviderPricingConfig:
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    version: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ProviderResilienceConfig:
    enabled: bool = True
    retry: ProviderRetryConfig = field(default_factory=ProviderRetryConfig)
    timeout: ProviderTimeoutConfig = field(default_factory=ProviderTimeoutConfig)
    circuit_breaker: ProviderCircuitConfig = field(default_factory=ProviderCircuitConfig)
    fallback: ProviderFallbackConfig = field(default_factory=ProviderFallbackConfig)


@dataclass(frozen=True)
class OpenAIConfig:
    base_url: str
    api_key: str
    model: str
    transport: Literal["auto", "chat_completions", "responses"] = "auto"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = (
        "medium"
    )
    capability_profile: Literal["auto", "openai", "openai_compatible"] = "auto"
    resilience: ProviderResilienceConfig = field(default_factory=ProviderResilienceConfig)
    budgets: ProviderBudgetConfig = field(default_factory=ProviderBudgetConfig)
    pricing: ProviderPricingConfig = field(default_factory=ProviderPricingConfig)


@dataclass(frozen=True)
class ModelProviderConfig:
    provider_type: Literal["openai", "openai_compatible"]
    base_url: str
    api_key_env: str
    transports: tuple[Literal["chat_completions", "responses"], ...]
    capability_profile: Literal["auto", "openai", "openai_compatible"] = "auto"


@dataclass(frozen=True)
class ModelTargetConfig:
    provider: str
    model: str
    transport: Literal["auto", "chat_completions", "responses"] = "auto"
    context_window: int | None = None
    tools: bool = True
    reasoning_efforts: tuple[str, ...] = (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    provider_state: bool = True
    structured_output: bool = True
    pricing: ProviderPricingConfig = field(default_factory=ProviderPricingConfig)


@dataclass(frozen=True)
class ModelRouteConfig:
    candidates: tuple[str, ...]
    fallback_on: tuple[str, ...] = (
        "connection",
        "timeout",
        "rate_limit",
        "server",
        "circuit_open",
    )
    max_fallback_hops: int = 2
    max_total_attempts: int = 6
    allow_cross_provider: bool = True


@dataclass(frozen=True)
class ModelGatewayConfig:
    enabled: bool = False
    default_route: str = "default"
    providers: dict[str, ModelProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelTargetConfig] = field(default_factory=dict)
    routes: dict[str, ModelRouteConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class LangfuseConfig:
    enabled: bool
    public_key: str
    secret_key: str
    base_url: str
    environment: str
    sample_rate: float
    debug: bool


@dataclass(frozen=True)
class FeishuChannelConfig:
    enabled: bool
    app_id: str
    app_secret: str
    encrypt_key: str
    verification_token: str
    allow_from: list[str]
    permission_mode: Literal["deny", "allow", "commands"] = "deny"
    allowed_bash_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChannelsConfig:
    feishu: FeishuChannelConfig


@dataclass(frozen=True)
class KBConfig:
    backend: str  # "lightrag" | "none"
    base_url: str  # LightRAG server URL (empty = KB disabled)
    api_key: str  # Optional auth token


@dataclass(frozen=True)
class VLMConfig:
    backend: str  # "paddleocr" | "none"
    api_url: str  # PaddleOCR async API base URL
    api_key: str  # PaddleOCR bearer token
    poll_interval: int  # Seconds between status polls
    timeout: int  # Max wait seconds


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    console: bool
    file: bool
    dir: str
    rotation: str
    retention: str


@dataclass(frozen=True)
class HooksConfig:
    debug: bool


@dataclass(frozen=True)
class WebSearchConfig:
    tavily_api_key: str


@dataclass(frozen=True)
class MemoryProcessingConfig:
    enabled: bool = False
    worker_interval_ms: int = 1_000
    max_attempts: int = 5


@dataclass(frozen=True)
class MemoryFeatureConfig:
    enabled: bool = False


@dataclass(frozen=True)
class MemoryCoreConfig:
    enabled: bool = False
    max_tokens: int = 1_200


@dataclass(frozen=True)
class MemoryContextManagerConfig:
    enabled: bool = False
    shadow_mode: bool = True
    max_input_tokens: int = 32_000
    reserved_output_tokens: int = 4_000
    max_episodic_results: int = 5
    max_semantic_results: int = 8
    subagent_episode_weight: float = 0.6


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    sync_interval_seconds: int = 3
    processing: MemoryProcessingConfig = field(default_factory=MemoryProcessingConfig)
    episodic: MemoryFeatureConfig = field(default_factory=MemoryFeatureConfig)
    semantic: MemoryFeatureConfig = field(default_factory=MemoryFeatureConfig)
    core: MemoryCoreConfig = field(default_factory=MemoryCoreConfig)
    context_manager: MemoryContextManagerConfig = field(
        default_factory=MemoryContextManagerConfig
    )


@dataclass(frozen=True)
class DaytonaConfig:
    api_key: str = ""
    server_url: str = ""
    target: str = ""
    default_workspace: str = "/workspace"


@dataclass(frozen=True)
class LocalGuardedConfig:
    enabled: bool = True
    require_isolated_shell: bool = False
    timeout_ms: int = 120_000
    max_timeout_ms: int = 600_000
    max_output_bytes: int = 2_000_000


@dataclass(frozen=True)
class DurableRunConfig:
    enabled: bool = True
    heartbeat_interval_ms: int = 5_000
    lease_duration_ms: int = 20_000
    max_attempts: int = 1


@dataclass(frozen=True)
class Config:
    openai: OpenAIConfig
    langfuse: LangfuseConfig
    channels: ChannelsConfig
    kb: KBConfig
    vlm: VLMConfig
    logging: LoggingConfig
    hooks: HooksConfig
    web_search: WebSearchConfig
    system_prompt: str
    db_path: str
    default_worktree: str
    default_permission_action: Literal["allow", "deny", "ask"]
    prompt_templates: dict[str, str]
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    daytona: DaytonaConfig = field(default_factory=DaytonaConfig)
    local_guarded: LocalGuardedConfig = field(default_factory=LocalGuardedConfig)
    durable_run: DurableRunConfig = field(default_factory=DurableRunConfig)
    model_gateway: ModelGatewayConfig = field(default_factory=ModelGatewayConfig)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

GLOBAL_CONFIG_PATHS = (
    "~/.nexa/config.yaml",
    "~/.nexa/config.json",
)


def project_config_paths(worktree: str) -> tuple[str, ...]:
    if not worktree:
        return ()
    return (
        os.path.join(worktree, ".nexa", "config.yaml"),
        os.path.join(worktree, ".nexa", "config.json"),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _load_yaml_file(path: str) -> dict[str, Any] | None:
    """Load a YAML or JSON file defensively. Returns None on any error."""
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        if p.suffix == ".json":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge; override wins for leaf values."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


# ---------------------------------------------------------------------------
# Defaults from schema
# ---------------------------------------------------------------------------


def _defaults_dict() -> dict[str, Any]:
    """Build a nested dict of defaults from CONFIG_FIELD_META."""
    d: dict[str, Any] = {}
    for key, meta in CONFIG_FIELD_META.items():
        parts = key.split(".")
        target = d
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = copy.deepcopy(meta.default)
    return d


# ---------------------------------------------------------------------------
# Build Config from merged dict
# ---------------------------------------------------------------------------


def _detect_worktree() -> str:
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if (p / ".git").exists():
            return str(p)
    return str(cwd)


def _load_default_prompt() -> str:
    prompt_file = Path(__file__).parent / "prompts" / "default.txt"
    if prompt_file.exists():
        return prompt_file.read_text().strip()
    return "You are Nexa, a capable personal agent."


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
    return default


def _coerce_int(v: Any, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(v))
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(v: Any, minimum: int = 0) -> int | None:
    if v in (None, ""):
        return None
    try:
        return max(minimum, int(v))
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(v: Any, minimum: float = 0.0) -> float | None:
    if v in (None, ""):
        return None
    try:
        return max(minimum, float(v))
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(v)))
    except (TypeError, ValueError):
        return default


def _coerce_str_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        items: list[str] = []
        text = v.replace("\r", "\n")
        for line in text.split("\n"):
            for token in line.split(","):
                s = token.strip()
                if s:
                    items.append(s)
        return items
    return []


_ROUTE_FALLBACK_CATEGORIES = {
    "connection",
    "timeout",
    "rate_limit",
    "server",
    "circuit_open",
    "model_unavailable",
}


def _build_model_gateway_config(raw: Any) -> ModelGatewayConfig:
    if not isinstance(raw, dict):
        return ModelGatewayConfig()
    enabled = _coerce_bool(raw.get("enabled", False), False)
    default_route = str(raw.get("default_route") or "default").strip()
    raw_providers = raw.get("providers") or {}
    raw_models = raw.get("models") or {}
    raw_routes = raw.get("routes") or {}
    for label, value in (
        ("providers", raw_providers),
        ("models", raw_models),
        ("routes", raw_routes),
    ):
        if not isinstance(value, dict):
            raise RuntimeError(f"model_gateway.{label} must be a mapping")

    providers: dict[str, ModelProviderConfig] = {}
    for provider_id, value in raw_providers.items():
        if not isinstance(value, dict):
            raise RuntimeError(
                f"model_gateway.providers.{provider_id} must be a mapping"
            )
        name = str(provider_id).strip()
        provider_type = str(value.get("type") or "openai_compatible").strip().lower()
        if provider_type not in ("openai", "openai_compatible"):
            raise RuntimeError(
                f"model_gateway.providers.{name}.type must be openai or openai_compatible"
            )
        base_url = str(value.get("base_url") or "").strip()
        api_key_env = str(value.get("api_key_env") or "").strip()
        if not name or not base_url or not api_key_env:
            raise RuntimeError(
                f"model_gateway.providers.{name or '<empty>'} requires base_url and api_key_env"
            )
        raw_transports = _coerce_str_list(
            value.get("transports", ["responses", "chat_completions"])
        )
        transports: list[Literal["chat_completions", "responses"]] = []
        for transport in raw_transports:
            if transport not in ("chat_completions", "responses"):
                raise RuntimeError(
                    f"model_gateway.providers.{name}.transports contains {transport!r}"
                )
            transports.append(transport)  # type: ignore[arg-type]
        if not transports:
            raise RuntimeError(
                f"model_gateway.providers.{name}.transports must not be empty"
            )
        profile = str(value.get("capability_profile") or "auto").strip().lower()
        if profile not in ("auto", "openai", "openai_compatible"):
            raise RuntimeError(
                f"model_gateway.providers.{name}.capability_profile is invalid"
            )
        providers[name] = ModelProviderConfig(
            provider_type=provider_type,  # type: ignore[arg-type]
            base_url=base_url,
            api_key_env=api_key_env,
            transports=tuple(transports),
            capability_profile=profile,  # type: ignore[arg-type]
        )

    models: dict[str, ModelTargetConfig] = {}
    for alias, value in raw_models.items():
        if not isinstance(value, dict):
            raise RuntimeError(f"model_gateway.models.{alias} must be a mapping")
        name = str(alias).strip()
        provider = str(value.get("provider") or "").strip()
        model_id = str(value.get("model") or "").strip()
        if provider not in providers:
            raise RuntimeError(
                f"model_gateway.models.{name}.provider references unknown provider {provider!r}"
            )
        if not name or not model_id:
            raise RuntimeError(
                f"model_gateway.models.{name or '<empty>'} requires model"
            )
        transport = str(value.get("transport") or "auto").strip().lower()
        if transport not in ("auto", "chat_completions", "responses"):
            raise RuntimeError(
                f"model_gateway.models.{name}.transport is invalid"
            )
        reasoning = tuple(
            item.lower()
            for item in _coerce_str_list(
                value.get(
                    "reasoning_efforts",
                    ["none", "low", "medium", "high", "xhigh", "max"],
                )
            )
        )
        if not reasoning or any(
            item not in ("none", "low", "medium", "high", "xhigh", "max")
            for item in reasoning
        ):
            raise RuntimeError(
                f"model_gateway.models.{name}.reasoning_efforts is invalid"
            )
        pricing = value.get("pricing") or {}
        if not isinstance(pricing, dict):
            raise RuntimeError(f"model_gateway.models.{name}.pricing must be a mapping")
        models[name] = ModelTargetConfig(
            provider=provider,
            model=model_id,
            transport=transport,  # type: ignore[arg-type]
            context_window=_coerce_optional_int(value.get("context_window"), 1),
            tools=_coerce_bool(value.get("tools", True), True),
            reasoning_efforts=reasoning,
            provider_state=_coerce_bool(value.get("provider_state", True), True),
            structured_output=_coerce_bool(
                value.get("structured_output", True), True
            ),
            pricing=ProviderPricingConfig(
                input_per_million=_coerce_optional_float(
                    pricing.get("input_per_million")
                ),
                cached_input_per_million=_coerce_optional_float(
                    pricing.get("cached_input_per_million")
                ),
                output_per_million=_coerce_optional_float(
                    pricing.get("output_per_million")
                ),
                version=str(pricing.get("version") or "").strip() or None,
                source=str(pricing.get("source") or "").strip() or None,
            ),
        )

    routes: dict[str, ModelRouteConfig] = {}
    for route_name, value in raw_routes.items():
        if not isinstance(value, dict):
            raise RuntimeError(
                f"model_gateway.routes.{route_name} must be a mapping"
            )
        name = str(route_name).strip()
        candidates = tuple(_coerce_str_list(value.get("candidates", [])))
        if not candidates:
            raise RuntimeError(
                f"model_gateway.routes.{name}.candidates must not be empty"
            )
        unknown = [candidate for candidate in candidates if candidate not in models]
        if unknown:
            raise RuntimeError(
                f"model_gateway.routes.{name} references unknown models: {', '.join(unknown)}"
            )
        fallback_on = tuple(
            item.lower()
            for item in _coerce_str_list(
                value.get("fallback_on", sorted(_ROUTE_FALLBACK_CATEGORIES))
            )
        )
        invalid = [item for item in fallback_on if item not in _ROUTE_FALLBACK_CATEGORIES]
        if invalid:
            raise RuntimeError(
                f"model_gateway.routes.{name}.fallback_on contains invalid categories: {', '.join(invalid)}"
            )
        routes[name] = ModelRouteConfig(
            candidates=candidates,
            fallback_on=fallback_on,
            max_fallback_hops=min(
                10,
                _coerce_int(value.get("max_fallback_hops", 2), 2, 0),
            ),
            max_total_attempts=min(
                30,
                _coerce_int(value.get("max_total_attempts", 6), 6, 1),
            ),
            allow_cross_provider=_coerce_bool(
                value.get("allow_cross_provider", True), True
            ),
        )

    if enabled:
        if not providers or not models or not routes:
            raise RuntimeError(
                "model_gateway.enabled requires providers, models, and routes"
            )
        if default_route not in routes:
            raise RuntimeError(
                f"model_gateway.default_route references unknown route {default_route!r}"
            )
    return ModelGatewayConfig(
        enabled=enabled,
        default_route=default_route,
        providers=providers,
        models=models,
        routes=routes,
    )


def _get(d: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Get a value from a nested dict by dotted key."""
    parts = dotted.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _build_config(merged: dict[str, Any]) -> Config:
    """Construct Config from a merged dict, applying defaults and coercion."""
    g = merged  # short alias

    openai = g.get("openai", {}) or {}
    base_url = str(openai.get("base_url", "") or "").strip()
    api_key = str(openai.get("api_key", "") or "").strip()
    model = str(openai.get("model", "") or "").strip()
    transport_raw = (
        str(openai.get("transport", "auto") or "auto")
        .strip()
        .lower()
    )
    transport: Literal["auto", "chat_completions", "responses"]
    if transport_raw in ("auto", "chat_completions", "responses"):
        transport = transport_raw  # type: ignore[assignment]
    else:
        raise RuntimeError(
            "openai.transport must be one of: auto, chat_completions, responses"
        )
    reasoning_effort_raw = (
        str(openai.get("reasoning_effort", "medium") or "medium").strip().lower()
    )
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"]
    if reasoning_effort_raw in ("none", "low", "medium", "high", "xhigh", "max"):
        reasoning_effort = reasoning_effort_raw  # type: ignore[assignment]
    else:
        raise RuntimeError(
            "openai.reasoning_effort must be one of: none, low, medium, high, xhigh, max"
        )

    if not base_url:
        raise RuntimeError("openai.base_url is required in config")
    if not api_key:
        raise RuntimeError("openai.api_key is required in config")
    if not model:
        raise RuntimeError("openai.model is required in config")

    capability_profile_raw = str(
        openai.get("capability_profile", "auto") or "auto"
    ).strip().lower()
    capability_profile: Literal["auto", "openai", "openai_compatible"]
    if capability_profile_raw in ("auto", "openai", "openai_compatible"):
        capability_profile = capability_profile_raw  # type: ignore[assignment]
    else:
        raise RuntimeError(
            "openai.capability_profile must be one of: auto, openai, openai_compatible"
        )

    resilience = openai.get("resilience", {}) or {}
    if not isinstance(resilience, dict):
        resilience = {}
    retry = resilience.get("retry", {}) or {}
    timeout = resilience.get("timeout", {}) or {}
    circuit = resilience.get("circuit_breaker", {}) or {}
    fallback = resilience.get("fallback", {}) or {}
    budgets = openai.get("budgets", {}) or {}
    pricing = openai.get("pricing", {}) or {}
    for value_name, value in (
        ("retry", retry),
        ("timeout", timeout),
        ("circuit_breaker", circuit),
        ("fallback", fallback),
        ("budgets", budgets),
        ("pricing", pricing),
    ):
        if not isinstance(value, dict):
            if value_name == "retry":
                retry = {}
            elif value_name == "timeout":
                timeout = {}
            elif value_name == "circuit_breaker":
                circuit = {}
            elif value_name == "fallback":
                fallback = {}
            elif value_name == "budgets":
                budgets = {}
            else:
                pricing = {}

    lf = g.get("langfuse", {}) or {}
    langfuse_sample_rate_raw = lf.get("sample_rate", 1.0)
    try:
        langfuse_sample_rate = float(langfuse_sample_rate_raw)
        if not 0.0 <= langfuse_sample_rate <= 1.0:
            langfuse_sample_rate = 1.0
    except (ValueError, TypeError):
        langfuse_sample_rate = 1.0

    system_prompt = str(g.get("system_prompt", "") or "").strip()
    if not system_prompt:
        system_prompt = _load_default_prompt()

    db_path = str(
        g.get("db_path", "./data/nexa.sqlite3") or "./data/nexa.sqlite3"
    ).strip()

    default_worktree = str(g.get("default_worktree", "") or "").strip()
    default_worktree = default_worktree if default_worktree else _detect_worktree()
    if not os.path.isabs(default_worktree):
        default_worktree = str(Path(default_worktree).resolve())

    dpa_raw = str(g.get("default_permission_action", "ask") or "ask").strip().lower()
    default_permission_action: Literal["allow", "deny", "ask"]
    if dpa_raw in ("allow", "deny", "ask"):
        default_permission_action = dpa_raw  # type: ignore[assignment]
    else:
        default_permission_action = "ask"

    log = g.get("logging", {}) or {}
    channels = g.get("channels", {}) or {}
    feishu = channels.get("feishu", {}) or {}
    feishu_permission_mode_raw = (
        str(
            feishu.get("permission_mode", feishu.get("permissionMode", "deny"))
            or "deny"
        )
        .strip()
        .lower()
    )
    feishu_permission_mode: Literal["deny", "allow", "commands"]
    if feishu_permission_mode_raw in ("deny", "allow", "commands"):
        feishu_permission_mode = feishu_permission_mode_raw  # type: ignore[assignment]
    else:
        feishu_permission_mode = "deny"

    kb = g.get("kb", {}) or {}
    vlm = g.get("vlm", {}) or {}
    hooks = g.get("hooks", {}) or {}
    ws = g.get("web_search", {}) or {}
    memory = g.get("memory", {}) or {}
    if not isinstance(memory, dict):
        memory = {}
    memory_processing = memory.get("processing", {}) or {}
    memory_episodic = memory.get("episodic", {}) or {}
    memory_semantic = memory.get("semantic", {}) or {}
    memory_core = memory.get("core", {}) or {}
    memory_context_manager = memory.get("context_manager", {}) or {}
    if not isinstance(memory_processing, dict):
        memory_processing = {}
    if not isinstance(memory_episodic, dict):
        memory_episodic = {}
    if not isinstance(memory_semantic, dict):
        memory_semantic = {}
    if not isinstance(memory_core, dict):
        memory_core = {}
    if not isinstance(memory_context_manager, dict):
        memory_context_manager = {}
    daytona = g.get("daytona", {}) or {}
    local_guarded = g.get("local_guarded", {}) or {}
    if not isinstance(local_guarded, dict):
        local_guarded = {}
    durable_run = g.get("durable_run", {}) or {}
    if not isinstance(durable_run, dict):
        durable_run = {}
    pt = g.get("prompt_templates", {}) or {}
    if not isinstance(pt, dict):
        pt = {}

    retry_max_attempts = _coerce_int(retry.get("max_attempts", 3), 3, 1)
    retry_base_delay_ms = _coerce_int(retry.get("base_delay_ms", 500), 500, 0)
    retry_max_delay_ms = _coerce_int(
        retry.get("max_delay_ms", 8_000), 8_000, 0
    )
    connect_timeout_ms = _coerce_int(
        timeout.get("connect_ms", 10_000), 10_000, 100
    )
    first_event_timeout_ms = _coerce_int(
        timeout.get("first_event_ms", 45_000), 45_000, 100
    )
    idle_stream_timeout_ms = _coerce_int(
        timeout.get("idle_stream_ms", 30_000), 30_000, 100
    )
    total_attempt_timeout_ms = _coerce_int(
        timeout.get("total_attempt_ms", 180_000), 180_000, 100
    )
    fallback_models = tuple(_coerce_str_list(fallback.get("models", [])))
    if retry_max_attempts > 10:
        raise RuntimeError("openai.resilience.retry.max_attempts must be <= 10")
    if retry_max_delay_ms < retry_base_delay_ms:
        raise RuntimeError(
            "openai.resilience.retry.max_delay_ms must be >= base_delay_ms"
        )
    if total_attempt_timeout_ms < max(
        connect_timeout_ms,
        first_event_timeout_ms,
        idle_stream_timeout_ms,
    ):
        raise RuntimeError(
            "openai.resilience.timeout.total_attempt_ms must be >= connect_ms, "
            "first_event_ms, and idle_stream_ms"
        )
    if model in fallback_models:
        raise RuntimeError(
            "openai.resilience.fallback.models must not contain the primary model"
        )

    return Config(
        openai=OpenAIConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            transport=transport,
            reasoning_effort=reasoning_effort,
            capability_profile=capability_profile,
            resilience=ProviderResilienceConfig(
                enabled=_coerce_bool(resilience.get("enabled", True), True),
                retry=ProviderRetryConfig(
                    max_attempts=retry_max_attempts,
                    base_delay_ms=retry_base_delay_ms,
                    max_delay_ms=retry_max_delay_ms,
                    max_retry_after_ms=_coerce_int(
                        retry.get("max_retry_after_ms", 30_000), 30_000, 0
                    ),
                ),
                timeout=ProviderTimeoutConfig(
                    connect_ms=connect_timeout_ms,
                    first_event_ms=first_event_timeout_ms,
                    idle_stream_ms=idle_stream_timeout_ms,
                    total_attempt_ms=total_attempt_timeout_ms,
                ),
                circuit_breaker=ProviderCircuitConfig(
                    enabled=_coerce_bool(circuit.get("enabled", True), True),
                    failure_threshold=_coerce_int(
                        circuit.get("failure_threshold", 5), 5, 1
                    ),
                    failure_window_ms=_coerce_int(
                        circuit.get("failure_window_ms", 60_000), 60_000, 1_000
                    ),
                    cooldown_ms=_coerce_int(
                        circuit.get("cooldown_ms", 30_000), 30_000, 1_000
                    ),
                ),
                fallback=ProviderFallbackConfig(
                    same_model_transport=_coerce_bool(
                        fallback.get("same_model_transport", True), True
                    ),
                    models=fallback_models,
                ),
            ),
            budgets=ProviderBudgetConfig(
                max_calls_per_run=_coerce_int(
                    budgets.get("max_calls_per_run", 30), 30, 1
                ),
                max_attempts_per_run=_coerce_int(
                    budgets.get("max_attempts_per_run", 60), 60, 1
                ),
                max_input_tokens_per_run=_coerce_optional_int(
                    budgets.get("max_input_tokens_per_run")
                ),
                max_output_tokens_per_run=_coerce_optional_int(
                    budgets.get("max_output_tokens_per_run")
                ),
                max_cost_microusd_per_run=_coerce_optional_int(
                    budgets.get("max_cost_microusd_per_run")
                ),
            ),
            pricing=ProviderPricingConfig(
                input_per_million=_coerce_optional_float(
                    pricing.get("input_per_million")
                ),
                cached_input_per_million=_coerce_optional_float(
                    pricing.get("cached_input_per_million")
                ),
                output_per_million=_coerce_optional_float(
                    pricing.get("output_per_million")
                ),
                version=str(pricing.get("version") or "").strip() or None,
                source=str(pricing.get("source") or "").strip() or None,
            ),
        ),
        langfuse=LangfuseConfig(
            enabled=_coerce_bool(lf.get("enabled", True), True),
            public_key=str(lf.get("public_key", "") or "").strip(),
            secret_key=str(lf.get("secret_key", "") or "").strip(),
            base_url=str(
                lf.get("base_url", "https://cloud.langfuse.com")
                or "https://cloud.langfuse.com"
            ).strip(),
            environment=str(
                lf.get("environment", "development") or "development"
            ).strip(),
            sample_rate=langfuse_sample_rate,
            debug=_coerce_bool(lf.get("debug", False), False),
        ),
        channels=ChannelsConfig(
            feishu=FeishuChannelConfig(
                enabled=_coerce_bool(feishu.get("enabled", False), False),
                app_id=str(feishu.get("app_id", feishu.get("appId", "")) or "").strip(),
                app_secret=str(
                    feishu.get("app_secret", feishu.get("appSecret", "")) or ""
                ).strip(),
                encrypt_key=str(
                    feishu.get("encrypt_key", feishu.get("encryptKey", "")) or ""
                ).strip(),
                verification_token=str(
                    feishu.get(
                        "verification_token", feishu.get("verificationToken", "")
                    )
                    or ""
                ).strip(),
                allow_from=_coerce_str_list(
                    feishu.get("allow_from", feishu.get("allowFrom", []))
                ),
                permission_mode=feishu_permission_mode,
                allowed_bash_commands=_coerce_str_list(
                    feishu.get(
                        "allowed_bash_commands", feishu.get("allowedBashCommands", [])
                    )
                ),
            )
        ),
        kb=KBConfig(
            backend=str(kb.get("backend", "lightrag") or "lightrag").strip(),
            base_url=str(kb.get("base_url", "") or "").strip(),
            api_key=str(kb.get("api_key", "") or "").strip(),
        ),
        vlm=VLMConfig(
            backend=str(vlm.get("backend", "none") or "none").strip(),
            api_url=str(vlm.get("api_url", "") or "").strip(),
            api_key=str(vlm.get("api_key", "") or "").strip(),
            poll_interval=int(vlm.get("poll_interval", 5) or 5),
            timeout=int(vlm.get("timeout", 1800) or 1800),
        ),
        logging=LoggingConfig(
            level=str(log.get("level", "INFO") or "INFO").strip().upper(),
            console=_coerce_bool(log.get("console", True), True),
            file=_coerce_bool(log.get("file", True), True),
            dir=str(log.get("dir", "./data/logs") or "./data/logs").strip(),
            rotation=str(log.get("rotation", "00:00") or "00:00").strip(),
            retention=str(log.get("retention", "7 days") or "7 days").strip(),
        ),
        hooks=HooksConfig(debug=_coerce_bool(hooks.get("debug", False), False)),
        web_search=WebSearchConfig(
            tavily_api_key=str(ws.get("tavily_api_key", "") or "").strip(),
        ),
        memory=MemoryConfig(
            enabled=_coerce_bool(memory.get("enabled", True), True),
            embedding_base_url=str(memory.get("embedding_base_url", "") or "").strip(),
            embedding_api_key=str(memory.get("embedding_api_key", "") or "").strip(),
            embedding_model=str(memory.get("embedding_model", "") or "").strip(),
            sync_interval_seconds=_coerce_int(
                memory.get("sync_interval_seconds", 3), 3, 1
            ),
            processing=MemoryProcessingConfig(
                enabled=_coerce_bool(memory_processing.get("enabled", False), False),
                worker_interval_ms=_coerce_int(
                    memory_processing.get("worker_interval_ms", 1_000),
                    1_000,
                    100,
                ),
                max_attempts=_coerce_int(
                    memory_processing.get("max_attempts", 5), 5, 1
                ),
            ),
            episodic=MemoryFeatureConfig(
                enabled=_coerce_bool(memory_episodic.get("enabled", False), False),
            ),
            semantic=MemoryFeatureConfig(
                enabled=_coerce_bool(memory_semantic.get("enabled", False), False),
            ),
            core=MemoryCoreConfig(
                enabled=_coerce_bool(memory_core.get("enabled", False), False),
                max_tokens=_coerce_int(
                    memory_core.get("max_tokens", 1_200), 1_200, 100
                ),
            ),
            context_manager=MemoryContextManagerConfig(
                enabled=_coerce_bool(
                    memory_context_manager.get("enabled", False), False
                ),
                shadow_mode=_coerce_bool(
                    memory_context_manager.get("shadow_mode", True), True
                ),
                max_input_tokens=_coerce_int(
                    memory_context_manager.get("max_input_tokens", 32_000),
                    32_000,
                    1_000,
                ),
                reserved_output_tokens=_coerce_int(
                    memory_context_manager.get("reserved_output_tokens", 4_000),
                    4_000,
                    256,
                ),
                max_episodic_results=_coerce_int(
                    memory_context_manager.get("max_episodic_results", 5),
                    5,
                    0,
                ),
                max_semantic_results=_coerce_int(
                    memory_context_manager.get("max_semantic_results", 8),
                    8,
                    0,
                ),
                subagent_episode_weight=_coerce_float(
                    memory_context_manager.get("subagent_episode_weight", 0.6),
                    0.6,
                    0.0,
                    1.0,
                ),
            ),
        ),
        system_prompt=system_prompt,
        db_path=db_path,
        default_worktree=default_worktree,
        default_permission_action=default_permission_action,
        prompt_templates={str(k): str(v) for k, v in pt.items()},
        daytona=DaytonaConfig(
            api_key=str(daytona.get("api_key", "") or "").strip()
            or str(os.getenv("DAYTONA_API_KEY", "")).strip(),
            server_url=str(daytona.get("server_url", "") or "").strip()
            or str(os.getenv("DAYTONA_SERVER_URL", "")).strip(),
            target=str(daytona.get("target", "") or "").strip()
            or str(os.getenv("DAYTONA_TARGET", "")).strip(),
            default_workspace=str(
                daytona.get("default_workspace", "/workspace") or "/workspace"
            ).strip()
            or "/workspace",
        ),
        local_guarded=LocalGuardedConfig(
            enabled=_coerce_bool(local_guarded.get("enabled", True), True),
            require_isolated_shell=_coerce_bool(
                local_guarded.get("require_isolated_shell", False), False
            ),
            timeout_ms=_coerce_int(
                local_guarded.get("timeout_ms", 120_000), 120_000, 1_000
            ),
            max_timeout_ms=_coerce_int(
                local_guarded.get("max_timeout_ms", 600_000), 600_000, 1_000
            ),
            max_output_bytes=_coerce_int(
                local_guarded.get("max_output_bytes", 2_000_000),
                2_000_000,
                1_024,
            ),
        ),
        durable_run=DurableRunConfig(
            enabled=_coerce_bool(durable_run.get("enabled", True), True),
            heartbeat_interval_ms=_coerce_int(
                durable_run.get("heartbeat_interval_ms", 5_000), 5_000, 250
            ),
            lease_duration_ms=_coerce_int(
                durable_run.get("lease_duration_ms", 20_000), 20_000, 1_000
            ),
            max_attempts=_coerce_int(
                durable_run.get("max_attempts", 1), 1, 1
            ),
        ),
        model_gateway=_build_model_gateway_config(g.get("model_gateway")),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load(worktree_hint: str = "") -> Config:
    """Scan global + project config files, merge, and build Config."""
    merged = _defaults_dict()

    for path in GLOBAL_CONFIG_PATHS:
        data = _load_yaml_file(path)
        if data:
            merged = _deep_merge(merged, data)

    # Determine worktree: use hint, or whatever merged dict has, or auto-detect
    wt = (
        worktree_hint
        or str(merged.get("default_worktree", "") or "").strip()
        or _detect_worktree()
    )
    for path in project_config_paths(wt):
        data = _load_yaml_file(path)
        if data:
            merged = _deep_merge(merged, data)

    return _build_config(merged)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Serialize Config back to a plain dict."""
    from dataclasses import asdict

    return asdict(cfg)


REDACTED_CONFIG_VALUE = "<redacted>"


def mask_sensitive(d: dict[str, Any]) -> dict[str, Any]:
    """Replace secrets with configuration metadata safe for API responses."""
    result = copy.deepcopy(d)
    for key, meta in CONFIG_FIELD_META.items():
        if not meta.sensitive:
            continue
        parts = key.split(".")
        target = result
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                break
            target = target[part]
        else:
            leaf = parts[-1]
            if isinstance(target, dict) and leaf in target:
                target[leaf] = {"configured": bool(target[leaf])}
    return result


def redact_sensitive(d: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Redact secret values in a raw config mapping and list configured fields."""
    result = copy.deepcopy(d)
    configured: list[str] = []
    for key, meta in CONFIG_FIELD_META.items():
        if not meta.sensitive:
            continue
        parts = key.split(".")
        target = result
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                break
            target = target[part]
        else:
            leaf = parts[-1]
            if isinstance(target, dict) and leaf in target and target[leaf]:
                target[leaf] = REDACTED_CONFIG_VALUE
                configured.append(key)
    return result, configured


def restore_redacted_sensitive(
    incoming: dict[str, Any], existing: dict[str, Any]
) -> dict[str, Any]:
    """Preserve an existing secret when a redacted raw-config placeholder is saved."""
    result = copy.deepcopy(incoming)
    for key, meta in CONFIG_FIELD_META.items():
        if not meta.sensitive:
            continue
        parts = key.split(".")
        incoming_target = result
        existing_target: Any = existing
        for part in parts[:-1]:
            if not isinstance(incoming_target, dict) or part not in incoming_target:
                break
            incoming_target = incoming_target[part]
            existing_target = (
                existing_target.get(part, {})
                if isinstance(existing_target, dict)
                else {}
            )
        else:
            leaf = parts[-1]
            if (
                isinstance(incoming_target, dict)
                and incoming_target.get(leaf) == REDACTED_CONFIG_VALUE
            ):
                previous = (
                    existing_target.get(leaf)
                    if isinstance(existing_target, dict)
                    else None
                )
                if previous:
                    incoming_target[leaf] = previous
                else:
                    incoming_target.pop(leaf, None)
    return result


def save_config(data: dict[str, Any], path: str) -> None:
    """Write a dict as YAML to the given path, creating parent dirs."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def generate_default_yaml() -> str:
    """Generate a commented YAML string from CONFIG_FIELD_META."""
    lines: list[str] = [
        "# NexaPilot configuration",
        "# See config.yaml.example for full reference",
        "",
    ]
    current_section = ""
    for key, meta in CONFIG_FIELD_META.items():
        parts = key.split(".")
        if len(parts) > 1 and parts[0] != current_section:
            current_section = parts[0]
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"# --- {current_section} ---")

        desc = f"# {meta.description}"
        if meta.choices:
            desc += f" ({' | '.join(meta.choices)})"
        if meta.sensitive:
            desc += " [sensitive]"
        lines.append(desc)

        # Build the key path
        if len(parts) == 1:
            val = _format_yaml_value(meta.default)
            lines.append(f"{parts[0]}: {val}")
        elif len(parts) == 2:
            # We group under section headers
            val = _format_yaml_value(meta.default)
            lines.append(f"# {parts[0]}.{parts[1]}: {val}")
        else:
            val = _format_yaml_value(meta.default)
            lines.append(f"# {key}: {val}")
        lines.append("")

    return "\n".join(lines)


def _format_yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        if not v:
            return '""'
        return f'"{v}"'
    if isinstance(v, dict):
        return "{}"
    return str(v)


def get_config_sources(worktree: str = "") -> list[dict[str, Any]]:
    """List discovered config file paths with exists/loaded status."""
    wt = worktree or _detect_worktree()
    sources: list[dict[str, Any]] = []

    for path in GLOBAL_CONFIG_PATHS:
        p = Path(path).expanduser()
        data = _load_yaml_file(path)
        sources.append(
            {
                "path": str(p),
                "scope": "global",
                "exists": p.is_file(),
                "loaded": data is not None,
            }
        )

    for path in project_config_paths(wt):
        p = Path(path)
        data = _load_yaml_file(path)
        sources.append(
            {
                "path": str(p),
                "scope": "project",
                "exists": p.is_file(),
                "loaded": data is not None,
            }
        )

    return sources
