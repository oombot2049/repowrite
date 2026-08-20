# Agent Loop

[Documentation](../README.md) · [中文](../zh-CN/architecture/agent-loop.md)

NexaPilot uses a controlled ReAct-style loop: the model alternates between deciding, calling tools, observing persisted results, and producing a final answer. Plans and todos may guide this loop, but they do not replace it.

## Control flow

```python
lock(session_id)
load(session, message_history)
publish_status("busy")

while True:
    guard(turn_limit, interrupt_signal)
    context = build_context(session, history)
    tools = tool_registry.schemas()

    async for event in provider.stream(context, tools):
        persist_and_publish(event)
        collect_tool_calls(event)

    if no_tool_calls:
        finalize_assistant_message()
        break

    guard(tool_call_limit)
    execute_tool_batch_with_policy()
    history = reload_persisted_history()

publish_status("idle")
```

This is explanatory pseudocode, not a second implementation. The source of truth is `SessionLoop`.

## Why history is reloaded

Provider output is streamed into Message Parts. Tool calls then create Tool Operations and tool-result Messages. Reloading guarantees the next model call is built from committed facts, including approval rejection, timeout, invalid arguments, truncated output, and errors.

## Message lifecycle

An Assistant Message is created before streaming completes. Parts are appended or updated as text, reasoning metadata, and tool calls arrive. The message is finalized when the provider turn ends. This supports live UI updates without repeatedly replacing one large JSON document.

## Termination and safety

The loop stops on final model output, explicit interruption, cancellation, turn/tool limits, provider failure, or unrecoverable runtime error. Durable Run state distinguishes terminal outcomes from a process that disappeared while busy.

An interrupt signal is checked between phases. A tool already producing an external side effect cannot always be safely replayed or rolled back; startup reconciliation therefore prefers a conservative interrupted state over automatic side-effect replay.

## Plan versus execution

Goal, Plan, Task, and Todo objects express desired work and progress. The actual model/tool interaction remains the Agent Loop. A plan task may trigger a Run; one Run may use several tools; and the model may update todos while working. They are related control surfaces, not synonyms.

Subagent delegation is a normal `task` tool call. It creates a Child Session and nested Run, then returns bounded child output to the parent as a Tool Result.
