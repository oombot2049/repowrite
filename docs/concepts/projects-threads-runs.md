# Projects, threads, and runs

[Documentation](../README.md) · [中文](../zh-CN/concepts/projects-threads-runs.md)

NexaPilot separates repository identity, conversation history, and execution attempts so each one can have the correct lifecycle.

```text
Project
└── Thread (stored as a Session)
    ├── Messages and Parts
    ├── Run 1
    └── Run 2
        ├── Steps
        ├── Tool Operations
        ├── Provider Attempts
        └── Artifacts
```

## Project

A Project identifies a local repository root. It groups threads and defines the default filesystem boundary. Adding a Project does not upload or copy repository contents.

The user-facing term is **Project**. Some internal and API types use **worktree** or **cwd** for execution paths; those paths describe where a particular Run or tool executes, not a second project entity.

## Thread

A Thread is a continuing conversation inside one Project. The persistence layer calls it a `session`. It owns ordered user, assistant, and tool history plus goals, todos, and permission rules.

Create another Thread when the objective or relevant context changes substantially. Continuing an existing Thread retains its prior messages even if other threads were used in between.

## Run

A Run is one execution attempt triggered by user input or a scheduled wake-up. One Run may include multiple model calls and tool batches. Asking a clarification and later receiving the answer normally creates a later Run in the same Thread.

The Run is the operational boundary for status, cancellation, heartbeat, budgets, provider attempts, artifacts, and evaluation. It is not equivalent to one HTTP request or one model call.

## Step and tool operation

A Step records a meaningful unit inside a Run, such as a model phase or tool batch. A tool operation records one concrete invocation and its arguments, approval state, output, error, and timing.

This separation answers different questions:

- Thread: what has the user and agent discussed?
- Run: what happened because of this trigger?
- Step: where is execution inside that Run?
- Tool operation: what external side effect or observation occurred?

See [Agent loop](../architecture/agent-loop.md) for the control flow and [Database schema](../reference/database-schema.md) for persistence.
