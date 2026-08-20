# Model routing and fallback

[Documentation](../README.md) · [中文](../zh-CN/architecture/model-routing-and-fallback.md) · [Configuration example](../examples/model-gateway.yaml)

This page describes the implemented multi-model gateway: how a logical route becomes executable provider calls, which failures may select another target, how partial output changes the safety boundary, and what is persisted for diagnosis.

## The contract in one sentence

A Route is an ordered, capability-checked list of model targets; the gateway retries a target, may change transport for the same model, and may then select another model or provider only while no semantic output has escaped.

This is availability routing, not priority scheduling. NexaPilot still executes an LLM request immediately; it does not provide a global weighted queue or tenant quota scheduler.

## Registry layers

| Layer | Answers | Examples |
| --- | --- | --- |
| Provider | Where and with which credential? | Base URL, environment-variable name, supported transports |
| Model target | Which deployable model and capabilities? | Model ID, context window, tools, reasoning levels, Provider State, pricing |
| Route | In what order and under which failure policy? | Candidate aliases, allowed error categories, hop and attempt budgets |

Model aliases decouple business intent from provider model IDs. A route can move from `coding-premium` to `coding-balanced` without callers knowing endpoint details.

## Request flow

```mermaid
sequenceDiagram
    participant Loop as Agent Loop
    participant Router as Model Router
    participant Gateway as Provider Gateway
    participant Adapter as Protocol Adapter
    participant Store as SQLite Ledger
    participant Bus as Event Bus

    Loop->>Router: system, history, tools, options
    Router->>Router: resolve route and filter capabilities
    Router-->>Gateway: ordered ProviderRequestPlan list
    Gateway->>Bus: llm.route.planned
    Gateway->>Store: create llm_call
    Gateway->>Adapter: stream via selected provider and transport
    alt transient failure before semantic output
        Gateway->>Store: finish attempt/call as failed
        Gateway->>Bus: llm.fallback.selected
        Gateway->>Store: create linked fallback llm_call
        Gateway->>Adapter: stream using next plan
    else semantic output was emitted
        Gateway->>Store: finish attempt/call as interrupted
        Gateway-->>Loop: ProviderCallFailed with partial_output
    else success
        Gateway->>Store: persist usage and estimated cost
        Gateway->>Bus: llm.call.completed
        Gateway-->>Loop: normalized LLM events
    end
```

### Step 1: resolve the logical route

`ModelRouter` loads `model_gateway.default_route`, or starts at a requested model alias when the alias exists. `max_fallback_hops` limits how many target transitions can be planned.

### Step 2: reject incompatible targets before HTTP

For every target the router checks:

- tool support when the request exposes tools;
- requested reasoning effort;
- structured-output support when a response format is requested;
- estimated input size against the configured context window;
- Provider State support;
- provider transport support;
- `allow_cross_provider`.

Rejected aliases and reasons appear in `llm.route.planned`. Input estimation is deliberately conservative and tokenizer-independent; provider usage is authoritative after execution.

Opaque Provider State deserves special treatment. Responses reasoning/state items belong to the provider that created them. When history contains such state, NexaPilot does not move the request across a provider boundary, even if another provider advertises Provider State support. This prevents silent continuity loss or foreign-state rejection.

### Step 3: expand transport plans

The existing `CapabilityResolver` expands each model target. With `transport: auto`, one model can produce `responses` followed by `chat_completions` when its profile allows same-model transport fallback. Each resulting `ProviderRequestPlan` contains provider, endpoint hash, model alias, concrete model, transport, pricing, route, and fallback policy.

### Step 4: execute retries inside one call

An `llm_call` represents one concrete provider/model/transport choice. Its attempts implement transient retry with bounded exponential backoff and full jitter. Timeouts, circuit state, Run budgets, and route `max_total_attempts` are checked before another attempt starts.

### Step 5: select the next plan

There are three different mechanisms:

| Mechanism | Changes | Ledger shape |
| --- | --- | --- |
| Retry | Attempt number only | Same `llm_call`, new `llm_call_attempt` |
| Transport fallback | Protocol adapter | New linked `llm_call`, same model alias |
| Model/provider fallback | Model alias and possibly endpoint | New linked `llm_call` |

The new call stores `fallback_from_call_id`; metadata records provider ID, model alias, route name, and fallback kind. Therefore one logical Agent step may own multiple LLM calls without losing causality.

## Failure policy

Routes use friendly categories that map to normalized provider errors:

| Route value | Normalized category | Default meaning |
| --- | --- | --- |
| `connection` | `connection` | DNS, socket, connection reset |
| `timeout` | `timeout` | Connect, first event, idle stream, or total attempt deadline |
| `rate_limit` | `rate_limited` | Provider throttling; `Retry-After` remains bounded |
| `server` | `server_error` | Retryable upstream 5xx |
| `circuit_open` | `circuit_open` | This target is temporarily unhealthy |
| `model_unavailable` | `not_found` | Deployment/model is absent at this endpoint |

Authentication, permission, invalid request, content policy, context overflow, protocol errors, cancellation, and exhausted budgets do not fall back unless a future explicit policy adds safe handling. Changing provider cannot repair most of these and can hide configuration or application defects.

## The semantic-output barrier

`TextDelta`, `ReasoningDelta`, `ToolCall`, and `ProviderState` are semantic output. Once any one is emitted, automatic retry and fallback stop. Mixing the beginning of one model response with the end of another would corrupt the assistant message or duplicate a tool side effect. The call becomes `interrupted`, and the Agent loop receives a partial-output failure instead of a fabricated seamless result.

`ResponseStarted` and usage metadata alone do not cross this barrier because they are transport metadata, not user-visible meaning.

## Budgets and circuit isolation

- `openai.resilience.retry.max_attempts` bounds attempts per concrete call.
- `model_gateway.routes.*.max_total_attempts` bounds attempts across the entire fallback chain.
- `openai.budgets` remains the Run-wide guard for calls, attempts, tokens, and estimated cost.
- Circuit keys include endpoint hash, model, and transport, so one broken deployment does not open every provider circuit.

## Persistence and events

The authoritative audit data is stored in `llm_calls` and `llm_call_attempts`. Endpoint URLs and secrets are not written to the call record; a stable endpoint hash and credential-free aliases are stored instead.

Important events are:

- `llm.route.planned`: accepted plans, rejected candidates, and chain attempt budget;
- `llm.call.planned`: concrete provider/model/transport;
- `llm.attempt.started`, `llm.attempt.retrying`: attempt lifecycle;
- `llm.fallback.selected`: source, destination, kind, and normalized reason;
- `llm.fallback.skipped`: a compatible target existed but the chain budget was exhausted;
- `llm.call.completed` or the persisted failed/interrupted status.

## Configuration and operation

Copy [the complete example](../examples/model-gateway.yaml), set every `providers.*.api_key_env` variable in the process environment, and enable `model_gateway`. Credentials are resolved only during startup and are never exposed by provider capability/status APIs.

The legacy `openai` section remains the bootstrap and resilience configuration. `openai.resilience.fallback.models` is deprecated and does not implement cross-model routing; use `model_gateway.routes.*.candidates`.

## Implementation map and limits

- Registry validation: `src/nexapilot/config.py`
- Capability filtering and plan expansion: `src/nexapilot/llm/routing.py`
- Retry, circuit, fallback, ledger, and events: `src/nexapilot/llm/gateway.py`
- Adapter construction and environment credentials: `src/nexapilot/api/app.py`
- Behavioral tests: `tests/test_model_gateway_config.py`, `tests/test_provider_gateway.py`

Implemented providers use OpenAI or OpenAI-compatible Chat Completions/Responses adapters. This feature does not yet provide a global request queue, latency/cost-based dynamic ranking, tenant quotas, traffic shadowing, or automatic context summarization for a smaller fallback window.
