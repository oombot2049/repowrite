# 工具执行架构

[中文文档](../README.md) · [English](../../architecture/tool-execution.md) · [核心概念](../concepts/tools-policy-permission.md)

工具执行是一条 Pipeline，不是把模型输出直接当作函数调用。

```text
Provider Tool Call
  → 组装流式参数
  → JSON/Schema 校验
  → Tool Registry 查找
  → Policy 决策
  → 可选 Permission Request
  → Path/Runtime Guard
  → 带 Timeout/Output Limit 的 Executor
  → 统一 Result 或 Error
  → 持久化 Tool Operation + Message
  → 下一模型 Turn
```

## Registry 与 Schema

内置工具在 `src/nexapilot/tools/` 实现统一契约。Registry 只暴露当前 Agent 有效工具。MCP Tool 也会被适配到相同 Schema 和执行策略。

未知 Tool、非法 JSON、缺少必填字段和 Schema 错误不会进入 Executor，而是成为结构化失败。模型可以在 Loop Limit 内修正参数。

## 副作用契约

每个内置工具除模型可见的 JSON Schema 外，还声明一个 `ToolContract`。该契约是执行层元数据，不是给模型的提示词：

| 字段 | 可选值 | Runtime 语义 |
| --- | --- | --- |
| `side_effect` | `none`、`local_write`、`external_write`、`destructive` | 声明工具可能修改的最大边界。 |
| `idempotency` | `safe`、`requires_key`、`unsafe` | 声明相同参数重复执行是否安全。 |
| `retry` | `never`、`transient_only` | 声明允许考虑的重试类别；它本身不会触发重放。 |
| `compensation` | `none`、`manual`、`tool` | 声明已提交副作用的对账或补偿方式。 |
| `approval_scope` | `once`、`arguments`、`session` | 限制批准决定能否及如何复用。 |

未声明契约的旧工具和 MCP 工具采用保守默认值：可能外部写入、不可幂等、禁止重试、人工补偿、仅单次批准。缺少元数据不会让远端或遗留工具意外获得重放或批准复用能力。

执行前，Tool Operation 会保存契约、是否提供幂等键以及派生的恢复动作；成功或失败结果继续保留这些证据，供审计和评估使用。

## Policy 优先级

Permission Rule 按确定顺序匹配权限类别和参数 Pattern。Agent Profile 收紧 Child 能力，与 Child 有关的 Parent deny 会继承；模型无法请求更宽松 Profile。

`ask` 创建持久化 Permission Request。用户同意或拒绝会解决 Request、更新 Operation 并唤醒 Loop。拒绝作为 Observation 返回，不会伪装成成功。

## Executor 边界

文件工具根据有效 Workspace 解析路径，除非显式获得 External Directory Permission，否则拒绝越界。Shell 执行限制 Timeout 和最大输出。配置后可通过 Daytona Adapter 使用独立 Runtime。

Local Guard 不是强 Sandbox。执行不可信代码需要 Container、VM 或托管 Sandbox，并配合网络和凭证控制。

## 失败与重试

Timeout、异常、非法输出和权限拒绝都会记录 Error Code 与有限输出。模型可以修改参数或选择替代工具。非幂等工具不能盲目自动重试；重试策略必须考虑部分副作用。

Run Budget 限制总 Tool Call。执行阶段会检查 Cancel/Interrupt，但已提交到外部系统的操作可能仍需人工协调。

启动恢复不会自动重放执行中的工具，而是记录以下决策之一：

- `safe_to_retry`：工具无副作用或明确幂等；
- `retry_with_same_key`：工具要求幂等键，且该调用已提供键；
- `manual_review`：无法证明重复执行安全。

前两种情况把 Operation 标为已中断，等待后续显式重试决策；`manual_review` 标为 `needs_review`。所有情况下 `automatic_replay` 都保持为 false。
