# Configuration

[Documentation](../README.md) · [中文](../zh-CN/getting-started/configuration.md) · [Full reference](../reference/configuration.md)

NexaPilot merges two YAML files in this order:

1. `~/.nexa/config.yaml` — user-wide defaults
2. `./.nexa/config.yaml` — project-local overrides

Project values win. This lets one machine share provider defaults while each checkout keeps its own database, logs, permissions, and feature flags.

## Minimum provider configuration

```yaml
openai:
  base_url: "https://api.openai.com/v1"
  api_key: "REPLACE_ME"
  model: "YOUR_MODEL"
  transport: "responses"
  reasoning_effort: "medium"

db_path: "./data/nexa.sqlite3"
default_permission_action: "ask"
```

Use `responses` for OpenAI models that support it. Use `chat_completions` for compatible endpoints that only implement `/v1/chat/completions`. `auto` lets the Provider Gateway select and, when configured, fall back between transports.

## Safe local defaults

- Keep `default_permission_action: ask` until you understand every enabled tool.
- Keep runtime data under a Git-ignored directory.
- Do not expose the local server to an untrusted network. The current product assumes a trusted local operator and does not provide tenant authentication.
- Enable optional integrations only after their credentials and failure behavior are understood.

## Feature flags

Memory has a basic local-file layer and independently controlled production projections. The example configuration keeps processing, episodic, semantic, core, and Context Manager features off. Enable them deliberately and verify `/memory/status`.

Provider resilience, durable Run lifecycle, local guarded execution, logging, Langfuse, Feishu, knowledge base, Tavily search, and Daytona each have separate sections. See the [configuration reference](../reference/configuration.md) for values and defaults.

## Validate effective configuration

```bash
uv run nexa doctor
uv run nexa config show
```

The Web configuration API redacts secret fields. Avoid copying raw local YAML into bug reports.
