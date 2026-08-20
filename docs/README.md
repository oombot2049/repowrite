# NexaPilot documentation

[Project home](../README.md) · [简体中文](zh-CN/README.md)

The documentation is organized by the reason you opened it. Start with a task-oriented guide; move to architecture for implementation details; use reference pages for exact contracts.

## Getting started

- [Installation](getting-started/installation.md) — install dependencies and prepare local configuration.
- [Configuration](getting-started/configuration.md) — configure a provider, storage, permissions, and optional features.
- [First agent run](getting-started/first-agent-run.md) — create a project and thread, run a task, and inspect the result.

## Concepts

- [Projects, threads, and runs](concepts/projects-threads-runs.md)
- [Tools, policy, and permission](concepts/tools-policy-permission.md)
- [Memory and context](concepts/memory-and-context.md)

Concept pages explain what an object means and why it exists. They intentionally avoid database and implementation detail.

## Guides

- [CLI](guides/cli.md)
- [Memory](guides/memory.md)
- [Subagents](guides/subagents.md)
- [MCP and Skills](guides/mcp-and-skills.md)
- [Agent evaluation](guides/evaluation.md)

Guides explain how to configure, operate, verify, and troubleshoot an implemented capability.

## Architecture

- [System overview](architecture/overview.md)
- [Agent loop](architecture/agent-loop.md)
- [Context and Memory](architecture/context-and-memory.md)
- [Tool execution](architecture/tool-execution.md)
- [Persistence and events](architecture/persistence-and-events.md)
- [Model routing and fallback](architecture/model-routing-and-fallback.md)

Architecture pages describe runtime boundaries, control flow, failure behavior, and relevant source modules.

## Reference

- [Configuration reference](reference/configuration.md)
- [Database schema](reference/database-schema.md)
- [Provider compatibility](reference/provider-compatibility.md)

Reference pages list precise values and contracts. Source code and tests remain authoritative when a document becomes stale.

## Executable examples

- [Evaluation datasets and fixtures](examples/README.md)

## Documentation rules

- Describe implemented behavior, not roadmap ideas.
- Keep English and Chinese pages aligned when public behavior changes.
- Link to one authoritative explanation instead of copying it between pages.
- Put commands and expected outcomes in guides; put exhaustive fields and values in reference pages.
- Never include API keys, personal paths, private repository names, or runtime data.
