# Memory and context

[Documentation](../README.md) · [中文](../zh-CN/concepts/memory-and-context.md)

**Conversation history** is the complete record of a Thread. **Memory** is a searchable or curated projection of useful information. **Context** is the bounded package assembled for one model call.

They are related but not interchangeable.

## The four sources

```text
Session Store       complete messages and tool facts
Episodic Memory     searchable summaries of past tasks
Semantic Memory     versioned facts, preferences, decisions, constraints
Core Memory         a small curated set included on every relevant call
```

The Session Store remains the conversation source of truth. Episodic and semantic records keep source session, Run, and message identifiers so they can be audited or rebuilt. Core Memory is a controlled projection, not an independent truth source.

## Context Manager

Before a model call, Context Manager:

1. loads the active Thread history;
2. estimates the input budget after reserving output space;
3. selects always-on Core Memory;
4. retrieves relevant episodic and semantic records;
5. truncates or excludes lower-value material;
6. appends system instructions, agent prompt, and tool schemas;
7. records what was selected before sending the provider request.

Automatic injection gives the model predictable background. `memory_search` remains useful when the model discovers that it needs a specific fact that was not selected automatically, or needs to broaden a query during a tool loop.

## Memory writes

Completed Run data enters a transactional outbox. A worker incrementally projects new message ranges into episodic and semantic candidates. Source checkpoints and idempotency keys prevent a Thread from being permanently skipped merely because the user switched between threads.

Subagents may read project Memory. Their conclusions are lower-trust candidates by default and should not silently become active semantic facts owned by the primary agent.

See the [Memory guide](../guides/memory.md) for operation and [Context and Memory architecture](../architecture/context-and-memory.md) for internals.
