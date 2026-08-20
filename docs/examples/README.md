# Executable examples

[Documentation home](../README.md) · [Agent evaluation guide](../guides/evaluation.md)

This directory contains machine-readable evaluation datasets and their fixtures:

- `agent-eval.json`: tool-assisted personal-agent evaluation suite.
- `memory-eval.json`: Memory retrieval/effectiveness evaluation suite.
- `agent-eval-fixtures/`: isolated repository inputs used by evaluation cases.

Validate and execute datasets through the NexaPilot evaluation CLI:

```bash
nexa eval validate --dataset docs/examples/agent-eval.json
nexa eval run --dataset docs/examples/agent-eval.json --output-dir eval-results
```

Fixtures must be deterministic, contain no secrets, and remain safe to copy into a temporary workspace.
