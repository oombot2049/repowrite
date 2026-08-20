# Context and Memory architecture

[Documentation](../README.md) · [中文](../zh-CN/architecture/context-and-memory.md) · [Concept](../concepts/memory-and-context.md)

Context Manager is the read path into the model. Memory Processor is the asynchronous write path out of completed work. Keeping them separate prevents prompt construction from mutating long-term facts.

## Write path

```text
Run/message transaction
        │
        ├── canonical Session Store records
        └── outbox event with source sequence
                         │
                   Memory Worker
                         │
              extract and normalize
                 ┌───────┴────────┐
          Episodic record    Semantic candidate
                 │                │
              FTS index     dedupe/version/status
                                  │
                            Core projection
```

Episodes encode goal, actions, outcome, and learned evidence for a bounded task. Semantic records encode a subject/predicate/value-style fact plus status, version, confidence, and provenance. Core blocks select a small high-value subset for regular inclusion.

Checkpointing uses source Message sequence rather than “a new Session was created.” If a user alternates between Session A and B, later completed Runs in A still enqueue new ranges. Idempotency makes worker retry safe.

## Read path

```text
Active Thread history
Core Memory
Episodic retrieval
Semantic retrieval
System and Agent prompts
Tool schemas
        │
  relevance + trust + recency
  token estimation and truncation
        │
   provider-ready messages
```

The Context Manager reserves output tokens first, retains required recent conversation and tool adjacency, injects Core Memory, retrieves query-relevant projections, then trims lower-priority material. Selection metadata is observable so prompt behavior can be evaluated.

The budget is a hard constraint. Oversized Memory is trimmed first. If the newest atomic Run is still too large, the Context Manager truncates text, reasoning, tool input/output metadata, and provider state in a copied provider projection while preserving Tool Call/Tool Result adjacency. SQLite source facts are never modified.

## Retrieval

SQLite FTS5 provides tokenized full-text search; BM25 ranks lexical matches. Optional embeddings add semantic similarity. Content hashes cache embeddings for unchanged normalized chunks: editing one chunk only computes a new vector for changed content, while unused cached vectors may remain until cleanup.

## Trust and governance

- Canonical messages are immutable evidence; projections are derived.
- Every projection retains source Session/Run/Message identifiers.
- Conflicting semantic facts are versioned instead of silently overwritten.
- Subagent facts enter as candidates with source type and lower default trust.
- Forget operations deactivate projected facts without falsifying source history.
- Shadow Mode records Context Manager choices without injecting them.

## Failure behavior

An outbox event and its source write commit together. If a worker fails, the event remains retryable with attempt/error metadata. A Memory failure must not erase the completed Run. Context construction can degrade to Session history when optional projections are unavailable.
