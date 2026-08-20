# CLI guide

[Documentation](../README.md) · [中文](../zh-CN/guides/cli.md)

Most commands use the running REST service. `serve`, `stop`, and part of `doctor` also manage or inspect the local process.

## Global options

```text
nexapilot [--json] [--base-url URL] [--version] [--help]
```

- `--base-url` defaults to `http://127.0.0.1:4096` and reads `NEXA_URL`.
- `--json` must appear before the subcommand; data goes to stdout and status/errors to stderr.

When running from the source checkout, prefix commands with `uv run`.

## Essential workflow

```bash
uv run nexa doctor
uv run nexa serve --port 4096
uv run nexa run "Explain the main module" --permission ask
```

For explicit session control:

```bash
nexa sessions create --worktree /path/to/project --title "Architecture review"
nexa agent send <session-id> "Map the request lifecycle"
nexa agent run <session-id>
nexa sessions messages <session-id>
nexa agent interrupt <session-id>
```

## Operational command groups

| Group | Purpose |
| --- | --- |
| `config` | Show merged/redacted values, sources, schema, or initialize YAML |
| `sessions` / `agent` | Create and inspect conversations; send, run, and interrupt |
| `permissions` | List pending requests and submit decisions |
| `logs` | List log files and query structured records |
| `cronjobs` | Create, enable, run, inspect, and delete scheduled wake-ups |
| `skills` | Discover effective Skills for a worktree |
| `mcp` | Inspect tools and connect, disconnect, or add MCP servers |
| `kb` | Inspect and query the optional knowledge-base adapter |
| `memory eval` | Run offline Memory retrieval evaluation |
| `eval` | Validate or execute end-to-end Agent datasets |

Use `nexapilot <group> --help` and `nexapilot <group> <command> --help` as the exact command reference; Typer generates them from the installed version.

## Machine-readable automation

```bash
nexapilot --json doctor
nexapilot --json sessions list
nexapilot --json config show
```

Do not parse Rich terminal output in automation. Prefer JSON and treat a non-zero exit code as failure.

## Common failures

- **Cannot reach server**: start `serve` or set `--base-url` to the correct instance.
- **Config command cannot bootstrap**: create `.nexa/config.yaml` from the example before starting the first server.
- **Task remains queued**: run `doctor`, inspect logs, and check Provider status.
- **Permission blocks automation**: use an isolated fixture and an explicit test permission mode; do not globally disable approvals for a real repository.
