# Subagent guide

[Documentation](../README.md) · [中文](../zh-CN/guides/subagents.md)

NexaPilot delegates focused work through the `task` tool. The built-in `explore` Subagent is read-oriented and intended for repository discovery, evidence collection, and bounded analysis.

## When delegation happens

The Primary Agent receives the `task` tool schema and the available Agent descriptions. The model decides to call it when a focused child investigation can reduce parent context or run independently. The runtime validates the request and Agent profile; the model cannot invent an unregistered Agent.

Typical input:

```json
{
  "description": "Trace persistence flow",
  "prompt": "Find where messages and parts are written. Return file paths and a concise sequence.",
  "subagent_type": "explore"
}
```

## Isolation model

- The child receives its own persisted Session and Run.
- Parent and child share the intended worktree/runtime boundary.
- The child only receives tools allowed by its Agent profile.
- Parent denies relevant to child execution are inherited.
- The parent receives final child text plus task metadata, not the entire child transcript or reasoning.
- Child Memory conclusions are lower-trust candidates rather than automatically active Primary Memory.

## Continue a child

Passing the prior child `session_id` resumes by appending a new user message and starting another child Run. It does not resume a suspended Python coroutine. The child must belong to the same root Session and use the same Agent type.

## Limits and failures

Agent profiles define turn, tool-call, timeout, and parallelism limits. If the next tool batch would exceed the budget, the runtime executes none of that batch and closes each call with a structured `tool_budget_exhausted` result. It then performs one finalization round with no tools exposed, allowing the child to summarize collected evidence and disclose uncertainty. The run becomes limit-exceeded only if that finalization round still violates the protocol.

Normal provider finish reasons such as `stop` map to child `completed`; timeout, interruption, permission blocking, and real runtime errors remain distinct terminal states. The parent receives the final summary and status metadata as tool output.

The console subscribes to the root Session tree with `scope=tree`. Child events carry `root_session_id`, `parent_session_id`, `parent_tool_call_id`, `agent`, and `session_kind`, and can be filtered by Primary/Subagents. Child Messages appear in the event timeline but never merge into the Primary conversation body.

## Current boundary

The built-in role is `explore`. NexaPilot does not currently implement autonomous Agent teams, a general A2A network, or indefinitely running background children. Add roles through the Agent Registry only when their prompt, tool allowlist, permission profile, budgets, and output contract are explicit.

See [Agent Loop](../architecture/agent-loop.md) and `src/nexapilot/agents/` for implementation.
