# Persistence and events

[Documentation](../README.md) · [中文](../zh-CN/architecture/persistence-and-events.md)

SQLite is the local source of truth for Projects, Sessions, Runs, Messages, operations, approvals, tasks, artifacts, provider telemetry, and Memory projections. The browser reconstructs its timeline from these records.

## Message and Part

`messages` stores the stable envelope: identity, Session, role, timestamps, Run association, and ordering. `parts` stores ordered typed content such as text, reasoning metadata, and provider state. Incremental Parts let streaming UI and crash recovery share the same facts.

Encrypted Responses provider state is replayed only in later model rounds of the same Run that produced it, preserving reasoning continuity around Tool Calls. New Runs retain the SQLite source fact but do not send old encrypted state back to the provider; cross-run replay can fail with `invalid_encrypted_content` because the state no longer belongs to the active request chain.

Tool invocation lifecycle is stored separately in `tool_operations` because it has execution state, arguments, approval, output, error, and timings that should not be inferred from display text.

## Durable Run

`runs` stores status, ownership, counters, error information, cancellation state, heartbeat, lease, and terminal metadata. `run_steps`, `llm_calls`, and `llm_call_attempts` explain how the Run reached its outcome.

On startup, an expired lease indicates abandoned work. Reconciliation marks uncertain execution conservatively rather than replaying arbitrary tools.

## Transactional Outbox

The system sometimes must save a fact and later trigger derived work. Writing the database and publishing an in-memory event separately creates a failure window. Transactional Outbox writes both the domain change and an `outbox_events` row in one SQLite transaction.

```text
BEGIN
  write final Run/message state
  write outbox event
COMMIT
       ↓
worker claims event
       ↓
project Memory / publish derived state
       ↓
mark processed or schedule retry
```

If the process crashes after commit, the event remains. If it crashes before commit, neither fact is visible. Workers use idempotency and claim/retry metadata because delivery is at least once, not exactly once.

## Event Bus versus database

The in-process Event Bus provides low-latency UI updates. It is not the source of truth and does not replace SQLite. A disconnected client can reload messages, operations, and Run state; transient events only improve responsiveness.

`GET /events?session_id=<id>&scope=session|tree` provides two isolation scopes. `session` sends only the exact Session; `tree` includes Primary and Child Sessions under the same root. Provider events that only carry `run_id` are resolved through the Run record. Events with neither a Session nor a resolvable Run are excluded from filtered streams to prevent cross-project leakage. The server enriches delivered events with root, parent, agent, and parent Tool Call context.

See [Database schema](../reference/database-schema.md) for table responsibilities.
