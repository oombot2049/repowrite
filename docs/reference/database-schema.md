# Database schema

[Documentation](../README.md) · [中文](../zh-CN/reference/database-schema.md)

NexaPilot uses SQLite in WAL mode. This page describes table responsibilities and important relationships; `src/nexapilot/store/sqlite.py` is the executable schema.

## Repository and conversation

| Table | Responsibility |
| --- | --- |
| `projects` | Stable project name, unique local root, open/update times |
| `sessions` | Thread title, worktree/cwd, permissions, runtime, Project and parent-child Agent links |
| `channel_sessions` | Maps external channel conversations to Sessions |
| `messages` | Ordered User/Assistant/Tool envelopes by Session and Run |
| `parts` | Typed incremental Message content stored as JSON |
| `todos` | Session-scoped lightweight work list |

`messages(session_id, sequence)` is unique. Parts point to a Message; Tool Result messages may also carry `tool_call_id` and `tool_name`.

## Run and provider lifecycle

| Table | Responsibility |
| --- | --- |
| `runs` | Durable execution status, counters, errors, heartbeat, cancellation, terminal metadata |
| `session_leases` | Exclusive active ownership and lease expiration |
| `run_steps` | Ordered execution phases inside a Run |
| `tool_operations` | Canonical Tool call target, input, result, status, backend, error, timings |
| `llm_calls` | Logical Provider calls and transport/fallback relationships |
| `llm_call_attempts` | Individual retries, latency milestones, error and usage data |
| `provider_circuits` | Persisted circuit-breaker state |
| `artifacts` | Run outputs and content metadata |

One Run may own several Steps; one model Step may produce several provider attempts; one tool batch may create several Tool Operations.

## Permission and execution planning

| Table | Responsibility |
| --- | --- |
| `permission_requests` | Pending/resolved `ask` decisions and Tool metadata |
| `permission_approvals` | Persisted always/remembered approval patterns |
| `goals` | Long-lived objective and active Plan |
| `task_plans` | Versioned Plan for a Goal |
| `plan_tasks` | Executable Task state, ownership, retry, result, error |
| `plan_task_dependencies` | Task dependency graph |
| `task_runtime_events` | Append-only Task state-transition audit |
| `agent_workspaces` | Child Agent Git worktree identity and cleanup state |

## Events and Memory

| Table | Responsibility |
| --- | --- |
| `outbox_events` | At-least-once derived-work queue with idempotency and retry |
| `memory_checkpoints` | Last processed Message sequence per processor and Session |
| `memory_episodes` | Goal/action/outcome/lesson projection for a source Run |
| `memory_episodes_fts` | FTS5 search index for Episodes |
| `semantic_memories` | Versioned typed fact with status, confidence, importance, and provenance |
| `semantic_memories_fts` | FTS5 search index for semantic facts |
| `core_memory_blocks` | Token-counted, prioritized always-on projection |

The separate local Markdown Memory index in `src/nexapilot/memory/store.py` uses `meta`, `files`, `chunks`, `chunks_fts`, and `embedding_cache`.

## Evaluation feedback

| Table | Responsibility |
| --- | --- |
| `run_feedback` | One immutable satisfaction/error signal per terminal Run; stores only the redacted comment |
| `eval_candidates` | Review-gated bad-case snapshot with redacted prompt/response, typed failure reasons, provenance, and review state |

Negative feedback and its candidate are inserted in one transaction. `pending`, `accepted`, and `rejected` are review states, not Baseline states; no row in these tables can automatically replace an evaluation baseline.

## Scheduled work

`cron_jobs` stores schedule, payload, next/last execution, and enabled state. `cron_job_runs` records each scheduler attempt and links its resulting Assistant Message and trace.

## Migration rule

The current store initializes and applies additive runtime migrations in code. Do not edit tables manually to change product state. Back up the database before diagnostics and use APIs for permission or Memory governance.
