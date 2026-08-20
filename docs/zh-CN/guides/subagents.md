# Subagent 使用指南

[中文文档](../README.md) · [English](../../guides/subagents.md)

NexaPilot 通过 `task` 工具委派聚焦任务。内置 `explore` Subagent 以只读能力为主，用于仓库探索、证据收集和边界明确的分析。

## 何时发生委派

Primary Agent 会获得 `task` Schema 和可用 Agent 描述。模型判断子调查可以降低 Parent Context 或独立执行时提出调用；Runtime 校验请求和 Agent Profile，模型不能虚构未注册 Agent。

```json
{
  "description": "跟踪持久化流程",
  "prompt": "找出 Message 和 Part 的写入位置，返回文件路径和简洁时序。",
  "subagent_type": "explore"
}
```

## 隔离模型

- Child 拥有独立持久化 Session 和 Run。
- Parent 与 Child 共享预期 Worktree/Runtime 边界。
- Child 只能使用 Agent Profile 允许的工具。
- Parent 中与 Child 有关的 deny 会被继承。
- Parent 只接收 Child 最终文本和 Task Metadata，不接收完整 Transcript 或 Reasoning。
- Child 的 Memory 结论默认是低信任 Candidate，不自动成为 Primary Active Memory。

## 继续 Child Session

传入之前的 `session_id` 会追加一条 User Message 并创建新的 Child Run，不是恢复挂起的 Python 协程。Child 必须属于同一个 Root Session，并且 Agent 类型一致。

## 限制与失败

Agent Profile 定义 Turn、Tool Call、Timeout 和并行限制。工具预算不足以执行下一批调用时，Runtime 不会继续产生副作用，也不会立刻丢弃已收集证据：它为未执行的 Tool Call 写入结构化 `tool_budget_exhausted` 结果，然后启动一次不暴露任何工具的 Finalization Round，让 Child 总结现有证据并声明不确定性。只有 Finalization Round 仍违反协议时才以超限失败。

正常 Provider Finish Reason（例如 `stop`）会映射为 Child `completed`；Timeout、Interrupt、权限阻断和真正的 Runtime Error 保持独立终态。Parent 将最终摘要与状态 Metadata 作为 Tool Output 决定后续行为。

控制台使用 `scope=tree` 订阅 Root Session 的事件树。Child Event 携带 `root_session_id`、`parent_session_id`、`parent_tool_call_id`、`agent` 和 `session_kind`，可按 Primary/Subagents 过滤；Child Message 只进入事件时间线，不会混入 Primary 对话正文。

## 当前边界

内置角色只有 `explore`。当前不实现自主 Agent Team、通用 A2A 网络和无限期后台 Child。新增角色前必须明确 Prompt、Tool Allowlist、Permission Profile、Budget 和输出契约。

实现参见 [Agent Loop](../architecture/agent-loop.md) 和 `src/nexapilot/agents/`。
