# Installation

[Documentation](../README.md) · [中文](../zh-CN/getting-started/installation.md)

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A local repository that NexaPilot may read and modify
- An OpenAI-compatible model endpoint and API key

## Install from source

```bash
git clone https://github.com/oombot2049/repowrite.git
cd repowrite
uv sync
```

`uv sync` creates the project virtual environment and installs runtime and development dependencies from the lock file.

## Create local configuration

NexaPilot reads user defaults from `~/.nexa/config.yaml` and project overrides from `./.nexa/config.yaml`. For a first run, create a project-local copy:

```bash
mkdir .nexa
cp config.yaml.example .nexa/config.yaml
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force .nexa
Copy-Item config.yaml.example .nexa/config.yaml
```

The `.nexa/` directory is ignored by Git. Keep API keys and generated data there; never commit them.

## Verify the installation

After completing [configuration](configuration.md), run:

```bash
uv run nexa doctor
```

The command checks configuration loading, provider connectivity, SQLite, MCP, Skills, knowledge-base status, and the effective worktree. Fix failed checks before starting a long-running task.

## Start the console

```bash
uv run nexa serve --port 4096
```

Then open [http://127.0.0.1:4096](http://127.0.0.1:4096). Continue with [First agent run](first-agent-run.md).
