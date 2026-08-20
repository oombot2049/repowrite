"""Config field metadata registry.

Self-contained module (no project imports) that defines CONFIG_FIELD_META —
a flat registry of all config fields keyed by dotted path.
Used by config loading (for defaults) and the Settings UI (for descriptions).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigFieldMeta:
    key: str
    description: str
    default: Any
    sensitive: bool = False
    choices: list[str] | None = None

    @property
    def type_name(self) -> str:
        if self.default is None:
            return "string"
        return type(self.default).__name__


CONFIG_FIELD_META: dict[str, ConfigFieldMeta] = {}


def _r(key: str, description: str, default: Any, *, sensitive: bool = False, choices: list[str] | None = None) -> None:
    CONFIG_FIELD_META[key] = ConfigFieldMeta(
        key=key, description=description, default=default,
        sensitive=sensitive, choices=choices,
    )


# --- OpenAI ---
_r("openai.base_url", "OpenAI-compatible base URL", "")
_r("openai.api_key", "API key for LLM provider", "", sensitive=True)
_r("openai.model", "Model identifier", "")
_r(
    "openai.transport",
    "OpenAI API transport",
    "auto",
    choices=["auto", "chat_completions", "responses"],
)
_r(
    "openai.capability_profile",
    "Capability profile used for transport and parameter validation",
    "auto",
    choices=["auto", "openai", "openai_compatible"],
)
_r("openai.resilience.enabled", "Enable the durable Provider Gateway", True)
_r("openai.resilience.retry.max_attempts", "Maximum attempts for one logical LLM call", 3)
_r("openai.resilience.retry.base_delay_ms", "Initial provider retry delay", 500)
_r("openai.resilience.retry.max_delay_ms", "Maximum provider retry delay", 8000)
_r("openai.resilience.retry.max_retry_after_ms", "Maximum accepted Retry-After delay", 30000)
_r("openai.resilience.timeout.connect_ms", "Provider connection deadline", 10000)
_r("openai.resilience.timeout.first_event_ms", "First semantic event deadline", 45000)
_r("openai.resilience.timeout.idle_stream_ms", "Maximum stream idle interval", 30000)
_r("openai.resilience.timeout.total_attempt_ms", "Total deadline for one provider attempt", 180000)
_r("openai.resilience.circuit_breaker.enabled", "Enable provider circuit breaker", True)
_r("openai.resilience.circuit_breaker.failure_threshold", "Failures required to open a circuit", 5)
_r("openai.resilience.circuit_breaker.failure_window_ms", "Circuit failure aggregation window", 60000)
_r("openai.resilience.circuit_breaker.cooldown_ms", "Circuit open cooldown", 30000)
_r("openai.resilience.fallback.same_model_transport", "Allow an explicit same-model transport fallback", True)
_r(
    "openai.resilience.fallback.models",
    "Deprecated; use model_gateway.routes.<name>.candidates",
    [],
)
_r("openai.budgets.max_calls_per_run", "Maximum logical LLM calls per Run", 30)
_r("openai.budgets.max_attempts_per_run", "Maximum provider attempts per Run", 60)
_r("openai.budgets.max_input_tokens_per_run", "Optional input token budget per Run", None)
_r("openai.budgets.max_output_tokens_per_run", "Optional output token budget per Run", None)
_r("openai.budgets.max_cost_microusd_per_run", "Optional estimated cost budget in micro-USD", None)
_r("openai.pricing.input_per_million", "Configured input USD per million tokens", None)
_r("openai.pricing.cached_input_per_million", "Configured cached-input USD per million tokens", None)
_r("openai.pricing.output_per_million", "Configured output USD per million tokens", None)
_r("openai.pricing.version", "Pricing table version", None)
_r("openai.pricing.source", "Pricing source label", None)
_r(
    "openai.reasoning_effort",
    "Reasoning effort for the Responses API",
    "medium",
    choices=["none", "low", "medium", "high", "xhigh", "max"],
)

# --- Multi-model gateway ---
_r("model_gateway.enabled", "Enable cross-model and cross-provider routing", False)
_r("model_gateway.default_route", "Default model route name", "default")
_r("model_gateway.providers", "Named OpenAI-style provider endpoints", {})
_r("model_gateway.models", "Named model targets and capabilities", {})
_r("model_gateway.routes", "Ordered model fallback routes", {})

# --- Top-level ---
_r("system_prompt", "Global system prompt (empty = load from prompts/default.txt)", "")
_r("db_path", "SQLite database path", "./data/nexa.sqlite3")
_r("default_worktree", "Default worktree (empty = auto-detect git root)", "")
_r("default_permission_action", "Default permission action for new sessions", "ask", choices=["ask", "allow", "deny"])

# --- Local Guarded Host Executor ---
_r("local_guarded.enabled", "Enable approved host-shell compatibility execution", True)
_r("local_guarded.require_isolated_shell", "Reject host shell and require an isolated runtime", False)
_r("local_guarded.timeout_ms", "Default host command timeout in milliseconds", 120000)
_r("local_guarded.max_timeout_ms", "Maximum host command timeout in milliseconds", 600000)
_r("local_guarded.max_output_bytes", "Maximum captured host command output in bytes", 2000000)
_r("durable_run.enabled", "Enable durable Run lifecycle and startup reconciliation", True)
_r("durable_run.heartbeat_interval_ms", "Run lease heartbeat interval in milliseconds", 5000)
_r("durable_run.lease_duration_ms", "Run and Session lease duration in milliseconds", 20000)
_r("durable_run.max_attempts", "Maximum durable execution attempts for one Run", 1)

# --- Langfuse ---
_r("langfuse.enabled", "Enable Langfuse tracing", True)
_r("langfuse.public_key", "Langfuse public key", "", sensitive=True)
_r("langfuse.secret_key", "Langfuse secret key", "", sensitive=True)
_r("langfuse.base_url", "Langfuse server URL", "https://cloud.langfuse.com")
_r("langfuse.environment", "Langfuse tracing environment tag", "development")
_r("langfuse.sample_rate", "Trace sample rate (0.0 - 1.0)", 1.0)
_r("langfuse.debug", "Enable Langfuse debug logging", False)

# --- Channels ---
_r("channels.feishu.enabled", "Enable Feishu channel adapter", False)
_r("channels.feishu.app_id", "Feishu app id", "", sensitive=True)
_r("channels.feishu.app_secret", "Feishu app secret", "", sensitive=True)
_r("channels.feishu.encrypt_key", "Feishu event encrypt key (optional)", "", sensitive=True)
_r("channels.feishu.verification_token", "Feishu event verification token (optional)", "", sensitive=True)
_r("channels.feishu.allow_from", "Allowed Feishu sender open_ids (empty = allow all)", [])
_r(
    "channels.feishu.permission_mode",
    "Permission policy for channel-triggered tool calls",
    "deny",
    choices=["deny", "allow", "commands"],
)
_r(
    "channels.feishu.allowed_bash_commands",
    "Allowed bash command patterns when permission_mode=commands (fnmatch patterns)",
    [],
)

# --- Logging ---
_r("logging.level", "Log level", "INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
_r("logging.console", "Enable console log output", True)
_r("logging.file", "Enable file log output", True)
_r("logging.dir", "Log file directory", "./data/logs")
_r("logging.rotation", "Log rotation schedule (Loguru syntax)", "00:00")
_r("logging.retention", "Log retention period (Loguru syntax)", "7 days")

# --- Knowledge Base ---
_r("kb.backend", "KB backend type", "lightrag", choices=["lightrag", "none"])
_r("kb.base_url", "KB server URL (empty = disabled)", "")
_r("kb.api_key", "KB auth token", "", sensitive=True)

# --- VLM ---
_r("vlm.backend", "VLM parser backend", "none", choices=["paddleocr", "none"])
_r("vlm.api_url", "VLM API base URL", "")
_r("vlm.api_key", "VLM bearer token", "", sensitive=True)
_r("vlm.poll_interval", "VLM status poll interval (seconds)", 5)
_r("vlm.timeout", "VLM max wait time (seconds)", 1800)

# --- Hooks ---
_r("hooks.debug", "Enable hook debug logging", False)

# --- Web Search ---
_r("web_search.tavily_api_key", "Tavily API key for web search", "", sensitive=True)

# --- Memory ---
_r("memory.enabled", "Enable local memory indexing and retrieval", True)
_r("memory.embedding_base_url", "OpenAI-compatible embeddings base URL", "")
_r("memory.embedding_api_key", "API key for memory embeddings", "", sensitive=True)
_r("memory.embedding_model", "Embedding model identifier for memory search", "")
_r("memory.sync_interval_seconds", "Background memory sync interval in seconds", 3)
_r("memory.processing.enabled", "Enable asynchronous memory processing", False)
_r("memory.processing.worker_interval_ms", "Memory worker polling interval in milliseconds", 1000)
_r("memory.processing.max_attempts", "Maximum attempts before a memory event is dead-lettered", 5)
_r("memory.episodic.enabled", "Enable episodic memory extraction and retrieval", False)
_r("memory.semantic.enabled", "Enable structured semantic memory", False)
_r("memory.core.enabled", "Enable governed core memory blocks", False)
_r("memory.core.max_tokens", "Maximum tokens reserved for core memory", 1200)
_r("memory.context_manager.enabled", "Enable Context Manager for provider requests", False)
_r("memory.context_manager.shadow_mode", "Build and observe context without using it", True)
_r("memory.context_manager.max_input_tokens", "Maximum estimated provider input tokens", 32000)
_r("memory.context_manager.reserved_output_tokens", "Tokens reserved for model output", 4000)
_r("memory.context_manager.max_episodic_results", "Maximum recalled episodes per request", 5)
_r("memory.context_manager.max_semantic_results", "Maximum recalled semantic facts per request", 8)

# --- Daytona Runtime ---
_r("daytona.api_key", "Daytona API key", "", sensitive=True)
_r("daytona.server_url", "Daytona server URL", "")
_r("daytona.target", "Daytona target environment", "")
_r("daytona.default_workspace", "Default workspace path for Daytona sessions", "/workspace")

# --- Prompt Templates ---
_r("prompt_templates", "Custom template variables for system prompt (dict)", {})
