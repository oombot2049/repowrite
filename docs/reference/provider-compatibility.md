# Provider compatibility

[Documentation](../README.md) · [中文](../zh-CN/reference/provider-compatibility.md)

NexaPilot exposes one internal streaming protocol over two OpenAI-style transports.

| Capability | Responses | Chat Completions |
| --- | --- | --- |
| Endpoint family | `/v1/responses` | `/v1/chat/completions` |
| Tool calls | Supported | Supported when endpoint implements function tools |
| Reasoning effort | `none` through `max`, subject to model | Conservative profile uses `none` |
| Provider state | Persisted/supported | Not available in the common protocol |
| Streaming | SSE from provider adapter | SSE from provider adapter |

SSE is the wire delivery mechanism for incremental events. Responses and Chat Completions define the request/response schema. They are different dimensions: both transports may stream over SSE.

## Capability profiles

- `openai`: prefers Responses, supports both transports, and treats Chat Completions reasoning conservatively.
- `openai_compatible`: prefers Chat Completions and assumes only the common denominator until contract tests prove more.
- `auto`: selects `openai` for `api.openai.com`; otherwise selects `openai_compatible`.

The capability resolver rejects a model/transport/tools/reasoning combination before network execution when the declared profile cannot support it. This prevents known-invalid calls such as function tools plus unsupported Chat Completions reasoning.

## `transport: auto`

With non-`none` reasoning, `auto` prefers Responses. Otherwise it uses the profile's preferred transport. When `same_model_transport` fallback is enabled, it may prepare the other transport as a fallback.

Fallback occurs only for eligible connection, timeout, rate-limit, server, or circuit-open categories and only before semantic output begins. Once text, reasoning, a tool call, or Provider State has been emitted, switching protocols could duplicate output or side effects and is therefore blocked.

## Normalized events

Adapters translate provider-specific chunks into internal events such as response-started, text delta, reasoning delta, tool call, Provider State, usage, and error. SessionLoop persists these as Message Parts and telemetry without depending on the original wire format.

## Provider State

Responses may return opaque state needed to continue model reasoning consistently. NexaPilot stores it in a Provider State Part because the client owns durable conversation recovery. If it is discarded, a later request may lose continuity, force full reconstruction, or be rejected by a provider expecting continuation state.

## Resilience

The Provider Gateway applies bounded retry, timeout phases, circuit breaking, Run budgets, transport fallback, usage accounting, and error classification. `llm_calls` represent logical plans; `llm_call_attempts` preserve each network attempt.

OpenAI-compatible services vary. Validate the exact endpoint and model with tools, streaming, cancellation, malformed arguments, and reasoning settings before production use.
