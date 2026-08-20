# 多模型路由与降级机制

[中文文档](../README.md) · [English](../../architecture/model-routing-and-fallback.md) · [配置样例](../../examples/model-gateway.yaml)

本文完整解释当前已经实现的多模型网关：一个逻辑 Route 如何变成真实 Provider 调用，什么故障可以切换目标，为什么输出一部分内容后必须停止降级，以及如何从数据库和事件还原整条执行链。

## 一句话理解

Route 是一组有顺序、经过能力校验的模型目标；Gateway 先重试当前目标，再尝试同模型协议切换，最后才可能切换模型或 Provider，而且只允许发生在任何语义内容尚未输出之前。

这是“可用性路由”，不是“优先级调度”。当前 LLM 请求仍然立即执行，没有全局请求队列、租户权重或配额调度器。

## 三层配置分别解决什么问题

| 层级 | 回答的问题 | 典型字段 |
| --- | --- | --- |
| Provider | 请求发到哪里、凭证从哪里取？ | Base URL、API Key 环境变量名、支持的 Transport |
| Model Target | 实际调用哪个模型、它会什么？ | Model ID、上下文窗口、Tool、Reasoning、Provider State、价格 |
| Route | 以什么顺序尝试、什么故障才降级？ | 候选别名、错误分类、最大跳数、总 Attempt 预算 |

Model Alias 把业务意图和厂商 Model ID 解耦。调用方只依赖 `coding-premium`，不知道它背后是哪家 Endpoint；Route 可以把它降级到 `coding-balanced`。

## 一次请求的完整流程

```mermaid
sequenceDiagram
    participant Loop as Agent Loop
    participant Router as Model Router
    participant Gateway as Provider Gateway
    participant Adapter as Protocol Adapter
    participant Store as SQLite Ledger
    participant Bus as Event Bus

    Loop->>Router: System、History、Tools、Options
    Router->>Router: 解析 Route 并做能力过滤
    Router-->>Gateway: 有序 ProviderRequestPlan
    Gateway->>Bus: 发布 llm.route.planned
    Gateway->>Store: 创建 llm_call
    Gateway->>Adapter: 按 Provider 和 Transport 发起流式请求
    alt 输出语义内容前发生瞬时故障
        Gateway->>Store: Attempt 和 Call 记录为 failed
        Gateway->>Bus: 发布 llm.fallback.selected
        Gateway->>Store: 创建有关联的降级 llm_call
        Gateway->>Adapter: 使用下一个 Plan 请求
    else 已经输出语义内容
        Gateway->>Store: Attempt 和 Call 记录为 interrupted
        Gateway-->>Loop: 返回带 partial_output 的失败
    else 成功
        Gateway->>Store: 保存 Usage 与估算成本
        Gateway->>Bus: 发布 llm.call.completed
        Gateway-->>Loop: 返回统一 LLM Event
    end
```

### 第一步：确定逻辑 Route

`ModelRouter` 读取 `model_gateway.default_route`。如果调用方指定的是已注册 Model Alias，则从该 Alias 开始。`max_fallback_hops` 限制最多能跨越多少个候选目标。

### 第二步：发 HTTP 前先过滤不兼容目标

Router 逐个检查：

- 本次请求带 Tool 时，目标是否支持 Tool；
- 是否支持请求的 Reasoning Effort；
- 请求结构化输出时是否支持 Response Format；
- 估算输入是否超过配置的 Context Window；
- 是否支持 Provider State；
- Provider 是否支持目标 Transport；
- 是否允许跨 Provider。

被拒绝的 Alias 和原因进入 `llm.route.planned` 事件。输入 Token 是与 tokenizer 无关的保守估算，实际执行后的 Provider Usage 才是事实来源。

Provider State 是特殊边界。Responses 的 Reasoning/State Item 是生成它的 Provider 的不透明状态。历史中一旦包含这类状态，即使另一个 Provider 宣称支持 Provider State，NexaPilot 也不会把请求跨 Provider 迁移，否则可能被上游拒绝，或更隐蔽地丢失推理连续性。

### 第三步：把一个模型展开成具体协议计划

已有 `CapabilityResolver` 会继续工作。目标配置为 `transport: auto` 时，如果 Profile 允许，同一个模型可以展开成 `responses` 和后备的 `chat_completions`。最终每个 `ProviderRequestPlan` 都包含 Provider、Endpoint Hash、Model Alias、真实 Model ID、Transport、价格、Route 和降级策略。

### 第四步：在一个 Call 内执行 Retry

一个 `llm_call` 代表一次确定的 Provider + Model + Transport 选择；其内部多个 `llm_call_attempt` 才是瞬时故障重试。重试使用有上限的指数退避与 Full Jitter，并在开始前检查 Timeout、Circuit、Run Budget 和 Route 总 Attempt 预算。

### 第五步：必要时选择下一个 Plan

必须区分三种机制：

| 机制 | 变化的内容 | 数据库表现 |
| --- | --- | --- |
| Retry | 只增加 Attempt 序号 | 同一 `llm_call`，新增 `llm_call_attempt` |
| Transport Fallback | 更换协议 Adapter | 新 `llm_call`，Model Alias 不变 |
| Model/Provider Fallback | 更换 Model Alias，可能更换 Endpoint | 新 `llm_call` |

新 Call 通过 `fallback_from_call_id` 指向前一个 Call；Metadata 保存 Provider ID、Model Alias、Route Name 和 Fallback Kind。因此一个 Agent Step 即使产生多个 LLM Call，也可以完整追溯因果链。

## 哪些错误允许降级

Route 使用易读分类，再映射为内部统一错误：

| Route 配置值 | 内部分类 | 含义 |
| --- | --- | --- |
| `connection` | `connection` | DNS、Socket、Connection Reset |
| `timeout` | `timeout` | Connect、First Event、Idle Stream 或 Total Attempt 超时 |
| `rate_limit` | `rate_limited` | 上游限流，`Retry-After` 仍受最大值约束 |
| `server` | `server_error` | 可重试的上游 5xx |
| `circuit_open` | `circuit_open` | 当前目标暂时被熔断 |
| `model_unavailable` | `not_found` | Endpoint 上不存在该模型或部署 |

认证失败、权限失败、参数错误、内容安全、上下文超限、协议错误、取消和预算耗尽默认都不降级。换 Provider 通常修不好这些错误，反而会掩盖配置或应用缺陷。

## 最重要的安全边界：Semantic Output Barrier

`TextDelta`、`ReasoningDelta`、`ToolCall` 和 `ProviderState` 都算语义输出。只要其中任何一种已经发出，自动 Retry 和 Fallback 都立即停止。

原因很直接：如果把模型 A 的开头和模型 B 的结尾拼起来，Assistant Message 会被污染；如果 ToolCall 被重新生成，还可能产生重复副作用。此时 Call 状态变成 `interrupted`，Agent Loop 收到明确的 Partial Output 失败，而不是一份伪装成完整成功的结果。

`ResponseStarted` 与 Usage 只是传输元数据，不算语义内容，所以它们本身不会越过这道屏障。

## 三层预算与熔断隔离

- `openai.resilience.retry.max_attempts`：单个 Concrete Call 的 Attempt 上限；
- `model_gateway.routes.*.max_total_attempts`：整条降级链共享的 Attempt 上限；
- `openai.budgets`：Run 维度的 Call、Attempt、Token 和成本总护栏；
- Circuit Key 包含 Endpoint Hash、Model 和 Transport，一个坏部署不会把全部 Provider 一起熔断。

## 如何审计和排障

事实记录保存在 `llm_calls` 与 `llm_call_attempts`。Call 不保存 Endpoint URL 和 Secret，只保存稳定 Endpoint Hash 与不含凭证的别名。

关键事件：

- `llm.route.planned`：可执行计划、被拒绝候选、整链 Attempt 预算；
- `llm.call.planned`：实际 Provider、Model、Transport；
- `llm.attempt.started`、`llm.attempt.retrying`：Attempt 生命周期；
- `llm.fallback.selected`：从哪里切到哪里、Fallback 类型、标准化原因；
- `llm.fallback.skipped`：存在兼容后备目标，但整链 Attempt 预算已经耗尽；
- `llm.call.completed`，或数据库中的 `failed` / `interrupted` 状态。

排障时按 Run ID 查询 Call，再沿 `fallback_from_call_id` 串起来；每个 Call 内按 Attempt Number 查看错误分类和重试间隔，就能还原“先重试了几次、为何换协议、为何换模型”。

## 如何配置和启动

复制[完整配置样例](../../examples/model-gateway.yaml)，在启动进程的环境里设置每个 `providers.*.api_key_env` 指定的变量，然后开启 `model_gateway.enabled`。API Key 只在启动时解析，不会由 Provider Capability/Status API 返回。

旧 `openai` 段仍负责 Bootstrap 和通用 Resilience 参数。`openai.resilience.fallback.models` 已废弃，不能实现真正的跨模型路由；应使用 `model_gateway.routes.*.candidates`。

## 源码位置与当前边界

- Registry 校验：`src/nexapilot/config.py`
- 能力过滤与 Plan 展开：`src/nexapilot/llm/routing.py`
- Retry、Circuit、Fallback、Ledger、Event：`src/nexapilot/llm/gateway.py`
- Adapter 初始化与环境变量凭证：`src/nexapilot/api/app.py`
- 行为测试：`tests/test_model_gateway_config.py`、`tests/test_provider_gateway.py`

当前支持 OpenAI/OpenAI-compatible 的 Chat Completions 与 Responses Adapter。尚未实现全局请求队列、基于实时延迟/成本的动态排序、租户配额、Shadow Traffic，以及为更小 Context Window 自动摘要上下文。这些不应与当前已实现能力混淆。
