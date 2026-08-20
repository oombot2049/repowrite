# Tools, policy, and permission

[Documentation](../README.md) · [中文](../zh-CN/concepts/tools-policy-permission.md)

NexaPilot keeps capability, system policy, and human consent separate.

## Tool

A Tool is an executable capability exposed to the model through a JSON schema. Examples include reading files, searching text, writing files, running shell commands, fetching a URL, searching the Web, querying Memory, or delegating a task.

The model proposes a tool name and arguments. That proposal is untrusted input until validation and policy checks succeed.

## Policy

Policy is the deterministic system decision produced from ordered permission rules. A rule matches a permission category and argument pattern, then returns:

- `allow` — execute without asking the user;
- `ask` — create a permission request and pause that operation;
- `deny` — do not execute.

Policy is decided by code, not by the model. Session rules, agent profiles, and inherited denies determine the effective decision.

## Permission

Permission is the user's answer to an `ask` decision. Approval allows that concrete request to continue; rejection becomes a denied tool result. The model then receives the result in conversation history and can explain the outcome, revise its plan, or choose another tool.

Approval is therefore not a second Policy Engine. It resolves one pending operation that Policy deliberately escalated.

## Local Guard

For host-shell execution, Local Guard applies compatibility limits such as timeouts and maximum output. It is a final executor boundary, not an operating-system sandbox and not a substitute for permission rules.

## Why the distinction matters

```text
Model proposes a tool call
          ↓
Schema and argument validation
          ↓
Policy → allow / ask / deny
          ↓
User decision when action=ask
          ↓
Executor guard and tool execution
          ↓
Persisted Tool Result returned to model
```

This makes denial and failure ordinary, observable results instead of exceptions that silently terminate the agent loop. See [Tool execution](../architecture/tool-execution.md).
