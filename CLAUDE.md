# NexaPilot contributor context

NexaPilot is a local-first agent runtime implemented in Python 3.11+ with FastAPI, SQLite, Typer, and a static Web console.

## Start here

- [Documentation center](docs/README.md)
- [Getting started](docs/getting-started/installation.md)
- [Architecture overview](docs/architecture/overview.md)
- [Configuration reference](docs/reference/configuration.md)

## Important modules

- `src/nexapilot/loop/`: agent loop and interruption.
- `src/nexapilot/llm/`: Chat Completions/Responses adapters and Provider Gateway.
- `src/nexapilot/tools/`: built-in tools and common registry.
- `src/nexapilot/permission/`: policy rules and approval flow.
- `src/nexapilot/store/`: SQLite source of truth.
- `src/nexapilot/memory/`: Memory projections and context composition.
- `src/nexapilot/agents/`: Primary/Subagent registry and Child Sessions.
- `src/nexapilot/api/`: HTTP API and application lifecycle.

## Development checks

```bash
uv sync
uv run pytest
uv run nexa doctor
```

Preserve existing user changes, keep secrets out of Git, and update the matching English and `docs/zh-CN` pages when changing public behavior.
