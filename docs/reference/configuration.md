# Configuration reference

[Documentation](../README.md) · [中文](../zh-CN/reference/configuration.md) · [Example](../../config.yaml.example)

Configuration is YAML. User defaults load from `~/.nexa/config.yaml`; `./.nexa/config.yaml` overrides them for the current checkout.

## Required provider fields

| Field | Values | Meaning |
| --- | --- | --- |
| `openai.base_url` | URL | OpenAI-compatible API root |
| `openai.api_key` | non-empty string | Provider credential; secret |
| `openai.model` | model ID | Default model |
| `openai.transport` | `auto`, `chat_completions`, `responses` | Protocol adapter selection |
| `openai.reasoning_effort` | `none`, `low`, `medium`, `high`, `xhigh`, `max` | Requested reasoning level |
| `openai.capability_profile` | `auto`, `openai`, `openai_compatible` | Capability resolver profile |

`auto` capability profile treats `api.openai.com` as OpenAI and other hosts conservatively as compatible endpoints.

## Provider resilience

`openai.resilience` contains:

- `retry`: maximum attempts and exponential-delay bounds;
- `timeout`: connect, first-event, idle-stream, and total-attempt limits;
- `circuit_breaker`: threshold, failure window, and cooldown;
- `fallback.same_model_transport`: allow a safe same-model transport fallback;
- `fallback.models`: deprecated; use `model_gateway.routes.*.candidates`.

`openai.budgets` limits calls, attempts, input/output tokens, and estimated micro-USD per Run. Unknown pricing remains unknown; it is never treated as zero.

## Multi-model gateway

`model_gateway` is optional and disabled by default. `providers` defines endpoints and environment-variable credential references; `models` defines deployable capabilities and pricing; `routes` defines ordered aliases, safe error categories, fallback hops, and an attempt budget shared by the complete chain. See [the mechanism](../architecture/model-routing-and-fallback.md) and [complete example](../examples/model-gateway.yaml).

## Runtime and storage

| Field | Default/choices | Meaning |
| --- | --- | --- |
| `db_path` | `./data/nexa.sqlite3` | Main SQLite database |
| `default_worktree` | auto-detected when omitted | Default repository root |
| `default_permission_action` | `ask`; also `allow`, `deny` | New Session fallback Policy |
| `local_guarded.enabled` | `true` | Host-shell compatibility guard |
| `local_guarded.require_isolated_shell` | `false` | Require isolated executor instead of host shell |
| `local_guarded.timeout_ms` | `120000` | Default command timeout |
| `local_guarded.max_timeout_ms` | `600000` | Maximum accepted timeout |
| `local_guarded.max_output_bytes` | `2000000` | Captured output ceiling |

`durable_run` configures heartbeat interval, lease duration, and maximum attempts. Startup reconciliation does not blindly replay uncertain tool side effects.

## Logging and observability

`logging` controls level, console/file sinks, directory, rotation, and retention. `langfuse` controls optional tracing, endpoint, environment, sample rate, and credentials.

## Memory

`memory.enabled` controls local Memory. Embedding endpoint/key/model are optional. `processing`, `episodic`, `semantic`, `core`, and `context_manager` have independent `enabled` flags. Context Manager also defines Shadow Mode, input/output budgets, and result limits.

## Optional integrations

| Section | Purpose |
| --- | --- |
| `channels.feishu` | Feishu app credentials, allowlist, and channel permission mode |
| `kb` | LightRAG-compatible knowledge-base endpoint |
| `vlm` | Optional document parser adapter |
| `web_search` | Tavily API key |
| `daytona` | Remote sandbox endpoint and defaults |
| `hooks` | Hook debugging |
| `prompt_templates` | Custom variables available to prompt templates |

Secret fields are redacted by public configuration APIs. Keep actual YAML outside version control. The canonical defaults and validation logic are in `config.yaml.example`, `src/nexapilot/config.py`, and `src/nexapilot/config_schema.py`.
