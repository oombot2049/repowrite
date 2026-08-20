# Memory 与 Context

[中文文档](../README.md) · [English](../../concepts/memory-and-context.md)

**Conversation History** 是 Thread 的完整记录；**Memory** 是可搜索或受控整理的有用信息 Projection；**Context** 是为一次模型调用组装出的有限输入。三者相关，但不能混为一谈。

## 四类来源

```text
Session Store       完整 Message 与 Tool 事实
Episodic Memory     过去任务的可搜索摘要
Semantic Memory     版本化事实、偏好、决策和约束
Core Memory         每轮常驻的少量受控信息
```

Session Store 始终是对话事实来源。Episodic 和 Semantic 记录保存来源 Session、Run、Message，便于审计和重建。Core Memory 是受控 Projection，不是独立事实源。

## Context Manager

每次模型调用前，Context Manager：

1. 加载当前 Thread 历史；
2. 预留输出空间并计算输入预算；
3. 选择常驻 Core Memory；
4. 检索相关 Episodic 和 Semantic Memory；
5. 截断或排除低价值内容；
6. 追加 System Prompt、Agent Prompt 和 Tool Schema；
7. 记录选择结果后调用 Provider。

自动注入提供可预测背景。`memory_search` 则用于模型在执行中发现需要某个未被自动选择的事实，或需要扩大检索范围的场景。

## Memory 写入

Run 完成数据先进入 Transactional Outbox，Worker 按新增 Message 区间增量生成 Episodic 和 Semantic Candidate。Checkpoint 与幂等键避免用户切换 Thread 后，某个旧 Thread 永久失去归档机会。

Subagent 可以读取项目 Memory，但其结论默认是低信任 Candidate，不应静默升级为 Primary Agent 的 Active Semantic Memory。

操作参见 [Memory 指南](../guides/memory.md)，内部实现参见 [Context 与 Memory 架构](../architecture/context-and-memory.md)。
