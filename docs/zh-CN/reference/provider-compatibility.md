# Provider 兼容性

[中文文档](../README.md) · [English](../../reference/provider-compatibility.md)

NexaPilot 在两种 OpenAI 风格 Transport 上提供统一内部流式协议。

| 能力 | Responses | Chat Completions |
| --- | --- | --- |
| Endpoint | `/v1/responses` | `/v1/chat/completions` |
| Tool Call | 支持 | Endpoint 实现 Function Tool 时支持 |
| Reasoning Effort | `none` 到 `max`，受模型限制 | 保守 Profile 只使用 `none` |
| Provider State | 支持并持久化 | 通用协议中不可用 |
| Streaming | Provider Adapter 使用 SSE | Provider Adapter 使用 SSE |

SSE 是增量事件的网络传输方式；Responses 与 Chat Completions 定义请求和响应 Schema。两者不是同一维度，两种协议都可以通过 SSE Streaming。

## Capability Profile

- `openai`：优先 Responses，支持两个 Transport，并保守处理 Chat Completions Reasoning。
- `openai_compatible`：优先 Chat Completions，在契约测试证明前只假设公共能力。
- `auto`：`api.openai.com` 选择 OpenAI，其余选择 OpenAI-compatible。

当 Profile 不支持所选 Model/Transport/Tools/Reasoning 组合时，Capability Resolver 会在网络请求前拒绝，避免已知非法组合。

## `transport: auto`

Reasoning 不是 `none` 时优先 Responses，否则使用 Profile Preferred Transport。开启 `same_model_transport` 后，可以准备另一 Transport 作为回退。

只有 Connection、Timeout、Rate Limit、Server、Circuit Open 等合格分类，并且尚未产生 Semantic Output 时才允许 Fallback。Text、Reasoning、Tool Call 或 Provider State 已经输出后切换协议可能重复内容或副作用，因此被禁止。

## 统一事件

Adapter 将 Provider Chunk 转为 Response Started、Text Delta、Reasoning Delta、Tool Call、Provider State、Usage、Error 等内部事件。SessionLoop 不依赖原始协议即可将其保存为 Message Part 和 Telemetry。

## Provider State

Responses 可能返回后续推理连续性所需的不透明状态。NexaPilot 将其保存为 Provider State Part，因为客户端负责对话持久化恢复。丢弃它可能导致后续请求失去连续性、必须完整重建，或被要求续接状态的 Provider 拒绝。

## 弹性

Provider Gateway 提供有界 Retry、分阶段 Timeout、Circuit Breaker、Run Budget、Transport Fallback、Usage Accounting 和 Error Classification。`llm_calls` 表示逻辑计划，`llm_call_attempts` 保存每次网络尝试。

OpenAI-compatible 服务差异很大，上线前必须针对具体 Endpoint/Model 验证 Tool、Streaming、Cancel、错误参数和 Reasoning。
