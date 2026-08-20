# 持久化与事件

[中文文档](../README.md) · [English](../../architecture/persistence-and-events.md)

SQLite 是 Project、Session、Run、Message、Operation、Approval、Task、Artifact、Provider Telemetry 和 Memory Projection 的本地事实库。浏览器通过这些记录重建时间线。

## Message 与 Part

`messages` 保存稳定 Envelope：ID、Session、Role、时间、Run 关联和顺序；`parts` 保存有序类型化内容，例如 Text、Reasoning Metadata 和 Provider State。增量 Part 让流式 UI 与崩溃恢复使用同一事实。

Responses 的加密 Provider State 只在产生它的同一个 Run 的后续 Model Round 中回放，用于保持 Tool Call 前后的 Reasoning 连续性。跨 Run 构建新请求时仍保留 SQLite 事实，但不会把旧加密 State 发回 Provider；否则 Provider 可能因 State 与当前请求链不匹配而返回 `invalid_encrypted_content`。

Tool 生命周期独立保存在 `tool_operations`，因为它包含参数、审批、输出、错误和耗时，不能依赖展示文本反推。

## Durable Run

`runs` 保存 Status、Owner、Counter、Error、Cancel、Heartbeat、Lease 和终态 Metadata；`run_steps`、`llm_calls`、`llm_call_attempts` 解释 Run 如何得到结果。

启动时过期 Lease 表示遗留工作，协调逻辑保守标记不确定执行，不重放任意 Tool。

## Transactional Outbox

系统有时需要保存事实，再触发派生工作。如果数据库写入与内存事件发布分开，会出现失败窗口。Transactional Outbox 在同一 SQLite 事务中写 Domain Change 和 `outbox_events`。

```text
BEGIN
  写最终 Run/Message
  写 Outbox Event
COMMIT
       ↓
Worker Claim Event
       ↓
生成 Memory / 发布派生状态
       ↓
标记完成或安排 Retry
```

Commit 后崩溃，Event 仍存在；Commit 前崩溃，两个事实都不可见。投递语义是 At Least Once 而非 Exactly Once，因此 Worker 使用幂等键和 Claim/Retry Metadata。

## Event Bus 与数据库

进程内 Event Bus 提供低延迟 UI 更新，但不是事实来源，也不能替代 SQLite。客户端断开后可以重载 Message、Operation 和 Run State；临时事件只负责改善实时体验。

`GET /events?session_id=<id>&scope=session|tree` 提供两种隔离范围：`session` 只发送精确 Session，`tree` 发送同一 Root 下的 Primary 与 Child Session。只有 `run_id` 的 Provider Event 会通过 Run 反查 Session；既没有 Session 也无法从 Run 解析的事件不会进入带 Session 过滤的流，避免跨项目泄漏。服务端统一补齐 Root、Parent、Agent 和 Parent Tool Call 上下文。

表职责参见[数据库 Schema](../reference/database-schema.md)。
