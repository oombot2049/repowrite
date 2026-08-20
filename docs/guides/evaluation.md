# Agent evaluation guide

[Documentation](../README.md) · [中文](../zh-CN/guides/evaluation.md) · [Examples](../examples/README.md)

Agent quality is more than the final sentence. NexaPilot evaluates the conversation and the resulting environment: files, tests, Git changes, tool trace, safety boundaries, latency, tokens, and estimated cost.

## Validate a dataset

```bash
nexa eval validate --dataset docs/examples/agent-eval.json
```

Validation checks schema, fixture paths, checker configuration, and budgets without running the Agent.

## Run the suite

```bash
nexa eval run \
  --dataset docs/examples/agent-eval.json \
  --output-dir eval-results
```

For every case, the runner copies its fixture into an isolated workspace, initializes a fresh Git baseline, invokes the same Session/Message/Run API used by the console, collects evidence, and runs deterministic checkers.

## What to check

- Run reached an expected terminal status.
- Final answer contains or excludes required content.
- Required files exist and contain expected text.
- A command exits with the expected code.
- Changed files remain inside an allowlist.
- Required tools were called and forbidden tools were not.
- Tool operations contain no unexpected errors.
- Expected Artifacts were produced.
- Model/tool calls, duration, tokens, and cost remain within budget.

Critical correctness and safety conditions should be hard gates. Weighted scores are useful for softer quality dimensions but must not hide an out-of-scope file modification.

## Baseline regression

```bash
nexa eval run \
  --dataset docs/examples/agent-eval.json \
  --output-dir eval-results/current \
  --baseline eval-results/baseline/report.json
```

Promote a report to baseline only after review. A failing run must never automatically replace the last trusted baseline.

## Online feedback and bad-case review

The console exposes immutable feedback controls on every terminal Assistant Run. Positive feedback is stored as a satisfaction signal. Negative feedback requires at least one typed reason (`incorrect`, `incomplete`, `instruction_not_followed`, `tool_failure`, `unsafe`, `outdated`, or `other`) and creates a `pending` bad-case candidate.

The write is deliberately review-gated:

1. The API resolves the canonical Run, trigger Message, and Assistant Message.
2. It removes control characters and redacts common tokens, credentials, email addresses, and user-profile paths from the candidate snapshot.
3. Feedback and the optional candidate are committed in one SQLite transaction. A retry with the same payload is idempotent; a conflicting second submission is rejected to preserve the audit trail.
4. A reviewer opens **Bad cases** in the observability panel and accepts or rejects the candidate.
5. Acceptance only moves the item into the reviewed bad-case pool. It does not create an executable checker and never promotes the source Run to the trusted baseline.

This separation matters for failed Runs: failures are valuable regression inputs, but they are evidence of a problem, not proof of expected behavior. A maintainer must still turn an accepted candidate into a deterministic Eval Case with fixtures, checks, and budgets before baseline review.

Relevant API endpoints:

- `POST /runs/{run_id}/feedback`
- `GET /runs/{run_id}/feedback`
- `GET /evaluation/feedback`
- `GET /evaluation/candidates?status=pending`
- `POST /evaluation/candidates/{candidate_id}/review`

Only redacted feedback and candidate snapshots are stored in the feedback tables. Canonical Messages remain the source of truth and retain their normal retention policy.

## Failure investigation

`report.json` stores machine-readable evidence; `report.md` summarizes failures for review. Failed case workspaces are retained so files and Git diff can be inspected. Use `--keep-workspaces` to retain successful cases as well.

Use multiple cases for tool selection, permission rejection, invalid arguments, tool timeout, provider retry, cancellation, persistence recovery, and Memory relevance—not only happy-path coding.
