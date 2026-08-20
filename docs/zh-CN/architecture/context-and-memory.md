# Context 与 Memory 架构

[中文文档](../README.md) · [English](../../architecture/context-and-memory.md) · [核心概念](../concepts/memory-and-context.md)

Context Manager 是进入模型的读取路径，Memory Processor 是已完成工作进入长期投影的异步写入路径。两者分离，避免 Prompt 构建过程修改长期事实。

## 写入链路

```text
Run/Message Transaction
        │
        ├── Canonical Session Store Record
        └── 包含 Source Sequence 的 Outbox Event
                         │
                   Memory Worker
                         │
                   提取与标准化
                 ┌───────┴────────┐
          Episodic Record    Semantic Candidate
                 │                │
              FTS Index     去重/版本/状态
                                  │
                            Core Projection
```

Episode 围绕一次任务保存 Goal、Action、Outcome 和证据；Semantic Record 保存类似 Subject/Predicate/Value 的事实，以及 Status、Version、Confidence 和 Provenance；Core Block 从中选择少量高价值内容常驻。

Checkpoint 依据 Source Message Sequence，而不是“新建 Session”事件。如果用户在 Session A、B 之间切换，A 后续完成的 Run 仍会产生新区间。幂等键保证 Worker Retry 安全。

## 读取链路

```text
Active Thread History
Core Memory
Episodic Retrieval
Semantic Retrieval
System / Agent Prompt
Tool Schema
        │
  Relevance + Trust + Recency
  Token Estimate + Truncation
        │
   Provider-ready Messages
```

Context Manager 先预留输出 Token，保留必要的近期对话和 Tool 邻接关系，再加入 Core、检索相关 Projection，并裁剪低优先级内容。选择 Metadata 可观测，便于评估 Prompt 行为。

预算是硬约束：Memory 超限时先裁剪 Memory；最近原子 Run 仍过大时，Context Manager 在复制出的 Provider Projection 中裁剪 Text、Reasoning、Tool Output/Input Metadata 和 Provider State，并保留 Tool Call/Tool Result 邻接。SQLite 原始事实不被修改。

## 检索

SQLite FTS5 提供分词全文检索，BM25 对词法匹配排序；可选 Embedding 增加语义相似度。内容 Hash 缓存不变 Chunk 的 Embedding：修改一个 Chunk 只为新内容计算向量，未使用的旧缓存可以延迟清理。

## 信任与治理

- Canonical Message 是不可变证据，Projection 是派生数据。
- 每条 Projection 保留 Source Session/Run/Message。
- 冲突 Semantic Fact 使用版本化，不静默覆盖。
- Subagent Fact 以 Source Type 和较低默认 Trust 进入 Candidate。
- Forget 停用投影事实，不伪造来源历史。
- Shadow Mode 记录选择结果但不注入 Prompt。

## 失败语义

Outbox Event 与来源写入同一事务。Worker 失败时事件保留 Retry Attempt 和 Error。Memory Failure 不能抹掉已完成 Run；可选 Projection 不可用时，Context 构建可以降级到 Session History。
