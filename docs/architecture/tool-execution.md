# Tool execution architecture

[Documentation](../README.md) · [中文](../zh-CN/architecture/tool-execution.md) · [Concept](../concepts/tools-policy-permission.md)

Tool execution is a pipeline, not a direct function call from model output.

```text
Provider Tool Call
  → assemble streamed arguments
  → JSON/schema validation
  → Tool Registry lookup
  → Policy decision
  → optional Permission request
  → path/runtime guard
  → executor with timeout/output limit
  → normalized result or error
  → Tool Operation + Message persistence
  → next model turn
```

## Registry and schemas

Built-in tools implement a common contract under `src/nexapilot/tools/`. The registry exposes only tools effective for the active Agent. MCP tools are adapted into the same model-facing schema and execution policy.

Unknown tools, malformed JSON, missing required fields, and schema violations do not reach an executor. They become structured failures that the model can correct in a later turn, subject to loop limits.

## Side-effect contract

Every built-in tool declares a `ToolContract` in addition to its model-facing JSON Schema. The contract is execution metadata, not prompt advice:

| Field | Values | Runtime meaning |
| --- | --- | --- |
| `side_effect` | `none`, `local_write`, `external_write`, `destructive` | Classifies the maximum expected mutation boundary. |
| `idempotency` | `safe`, `requires_key`, `unsafe` | States whether repeating identical arguments is safe. |
| `retry` | `never`, `transient_only` | Declares the retry category; it does not itself replay a call. |
| `compensation` | `none`, `manual`, `tool` | Describes how a committed effect can be reconciled. |
| `approval_scope` | `once`, `arguments`, `session` | Limits whether an approval can be reused. |

Undeclared and MCP tools use a conservative contract: possible external write, unsafe idempotency, no retry, manual compensation, and one-call approval. This prevents a legacy or remote tool from gaining replay or approval reuse merely because metadata is absent.

The contract, idempotency-key presence, and derived recovery action are written into Tool Operation metadata before execution. Completed and failed results preserve the same evidence for audit and evaluation.

## Policy precedence

Permission rules match tool permission categories and argument patterns in deterministic order. Agent profiles reduce child capability; relevant parent denies are inherited by Subagents. A model cannot request a more permissive profile.

`ask` creates a durable Permission Request. Approval or rejection resolves that request, updates the operation, and wakes the loop. Rejection is returned as an observation rather than pretending the tool succeeded.

## Executor boundaries

Filesystem tools resolve paths against the effective workspace and reject traversal outside the allowed root unless an explicit external-directory permission permits it. Shell execution applies timeout and output limits. Daytona uses a separate runtime adapter when configured.

Local Guard is intentionally not presented as a strong sandbox. Production use with untrusted code requires an isolation boundary such as a container, VM, or managed sandbox plus network and credential controls.

## Failures and retries

Tool timeout, exception, invalid output, and permission denial are persisted with error codes and bounded output. The model may revise arguments or choose an alternative. Automatic executor retry is unsafe for non-idempotent tools; retry policy belongs to the tool contract and must account for possible partial side effects.

Run budgets cap total tool calls. Cancellation and interrupt signals are checked around execution, but a host operation already committed externally may require human reconciliation.

Startup reconciliation never automatically replays an in-flight tool. It records one of three decisions instead:

- `safe_to_retry`: the contract is side-effect-free or idempotent;
- `retry_with_same_key`: the contract requires an idempotency key and that call supplied one;
- `manual_review`: the side effect cannot be proven safe to repeat.

The first two decisions mark the operation interrupted but retryable by an explicit later decision. `manual_review` produces `needs_review`. `automatic_replay` remains false in all cases.
