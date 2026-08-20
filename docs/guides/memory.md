# Memory guide

[Documentation](../README.md) · [中文](../zh-CN/guides/memory.md) · [Concept](../concepts/memory-and-context.md)

NexaPilot can combine local Markdown Memory with optional episodic, semantic, core, and Context Manager projections. The production projections are feature-flagged so they can be enabled and observed independently.

## Enable the pipeline

```yaml
memory:
  enabled: true
  processing:
    enabled: true
  episodic:
    enabled: true
  semantic:
    enabled: true
  core:
    enabled: true
    max_tokens: 1200
  context_manager:
    enabled: true
    shadow_mode: false
    max_input_tokens: 32000
    reserved_output_tokens: 4000
```

Start in `shadow_mode: true` when validating selection without changing live prompts. Switch it off only after logs and offline evaluation show acceptable relevance and token use.

Embeddings are optional. When `embedding_base_url`, `embedding_api_key`, and `embedding_model` are empty, lexical FTS5/BM25 retrieval remains available.

## Observe processing

The Web API exposes:

- `GET /memory/status` — feature flags, worker and projection status;
- `GET /memory/outbox` — pending or failed projection events;
- `POST /memory/process` — request processing;
- `GET /memory/episodes` and `/memory/semantic` — inspect projected records;
- `GET /memory/core` and `POST /memory/core/rebuild` — inspect or rebuild Core Memory.

Semantic records can be activated or forgotten through governance endpoints. Keep source identifiers when investigating why a fact exists.

## Search during a Run

`memory_search` finds relevant chunks or projections. `memory_get` retrieves exact content after search. These tools complement automatic Context Manager selection; they do not replace it.

## Evaluate retrieval

```bash
nexa memory eval \
  --dataset docs/examples/memory-eval.json \
  --db-path ./data/nexa.sqlite3 \
  --min-recall 0.8
```

Use stable queries and expected source identifiers. Retrieval quality, prompt token cost, stale-fact rate, and provenance coverage should be reviewed together.

## Operational rules

- Treat Session Store as the source of conversation truth.
- Never edit projection tables as if they were primary history.
- Prefer forgetting/versioning to destructive source-history mutation.
- Keep Subagent-derived facts as candidates until a trusted path activates them.
- Back up the SQLite database before manual governance operations.

Implementation details are in [Context and Memory architecture](../architecture/context-and-memory.md).
