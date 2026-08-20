# First agent run

[Documentation](../README.md) · [中文](../zh-CN/getting-started/first-agent-run.md)

This walkthrough verifies the complete local path: project selection, thread creation, model streaming, tool policy, persistence, and final output.

## 1. Start the service

```bash
uv run nexa serve --port 4096
```

Open [http://127.0.0.1:4096](http://127.0.0.1:4096).

## 2. Add a project

Choose an existing local repository. A Project is the persistent identity and root boundary of that repository; it is not a copy of the source tree. NexaPilot stores its metadata in SQLite and uses the selected directory as the default working boundary.

## 3. Create a thread

A Thread is a continuing conversation inside one Project. It owns messages and can contain multiple Runs. Use separate threads for unrelated objectives so the model does not receive irrelevant history.

## 4. Send a bounded task

Start with a read-only request:

```text
Inspect this repository, identify the main entry points, and explain how to run its tests. Do not modify files.
```

The console creates a Run and streams its timeline. You may see model parts and read/search tool operations before the final response.

## 5. Exercise approval

With permission mode set to `ask`, send a small write request:

```text
Create a file named nexapilot-smoke-test.txt containing "NexaPilot is ready".
```

When the write tool requests approval, inspect its arguments. Approving executes it; rejecting records a denied tool result and returns control to the model, which may explain the denial or choose a safe alternative.

## 6. Verify persistence

Refresh the page or restart the service. The Project, Thread, messages, Run, tool operation, and final response should reload from SQLite. An abandoned active Run is reconciled conservatively rather than replaying uncertain side effects.

## CLI alternative

```bash
uv run nexa run "Summarize this repository" --permission ask
```

Use `doctor` first when the task remains queued or the provider fails before the first event.
