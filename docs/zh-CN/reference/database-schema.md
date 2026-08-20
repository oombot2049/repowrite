# 数据库 Schema

[中文文档](../README.md) · [English](../../reference/database-schema.md)

NexaPilot 使用 WAL 模式 SQLite。本页描述表职责和关键关系；可执行 Schema 以 `src/nexapilot/store/sqlite.py` 为准。

## 仓库与对话

| 表 | 职责 |
| --- | --- |
| `projects` | Project 名称、唯一本地 Root、打开与更新时间 |
| `sessions` | Thread 标题、Worktree/Cwd、权限、Runtime、Project 和 Parent/Child Agent 关系 |
| `channel_sessions` | 外部 Channel Conversation 到 Session 的映射 |
| `messages` | 按 Session/Run 排序的 User/Assistant/Tool Envelope |
| `parts` | JSON 格式的类型化增量 Message Content |
| `todos` | Session 级轻量工作清单 |

`messages(session_id, sequence)` 唯一；Part 指向 Message；Tool Result Message 还可记录 `tool_call_id`、`tool_name`。

## Run 与 Provider 生命周期

| 表 | 职责 |
| --- | --- |
| `runs` | 状态、Counter、Error、Heartbeat、Cancel、终态 Metadata |
| `session_leases` | 活跃执行的排他 Owner 与 Lease |
| `run_steps` | Run 内有序执行阶段 |
| `tool_operations` | Tool Target、Input、Result、Status、Backend、Error、Timing |
| `llm_calls` | 逻辑 Provider Call 与 Transport/Fallback 关系 |
| `llm_call_attempts` | 每次 Retry 的延迟节点、Error 与 Usage |
| `provider_circuits` | 持久化 Circuit Breaker State |
| `artifacts` | Run 产物和内容 Metadata |

一个 Run 可以包含多个 Step；一个模型 Step 可以产生多个 Provider Attempt；一个 Tool Batch 可以产生多个 Operation。

## 权限、计划与 Agent

| 表 | 职责 |
| --- | --- |
| `permission_requests` | `ask` 请求和 Tool Metadata |
| `permission_approvals` | 记住的 Approval Pattern |
| `goals` | 长期目标与 Active Plan |
| `task_plans` | Goal 的版本化 Plan |
| `plan_tasks` | Task State、Owner、Retry、Result、Error |
| `plan_task_dependencies` | Task 依赖图 |
| `task_runtime_events` | Append-only Task 状态审计 |
| `agent_workspaces` | Child Agent Git Worktree 与清理状态 |

## Event 与 Memory

| 表 | 职责 |
| --- | --- |
| `outbox_events` | 带幂等与 Retry 的 At-least-once 派生任务队列 |
| `memory_checkpoints` | Processor/Session 已处理 Message Sequence |
| `memory_episodes` / `_fts` | Run 经验 Projection 与全文索引 |
| `semantic_memories` / `_fts` | 版本化事实、状态、可信度、来源与索引 |
| `core_memory_blocks` | 带 Token Count 和 Priority 的常驻 Projection |

`src/nexapilot/memory/store.py` 的本地 Markdown Memory 另使用 `meta`、`files`、`chunks`、`chunks_fts`、`embedding_cache`。

## 评估反馈

| 表 | 职责 |
| --- | --- |
| `run_feedback` | 每个终态 Run 一份不可变满意度/错误信号，只保存脱敏后的 Comment |
| `eval_candidates` | 带人工门禁的 Bad Case 快照，保存脱敏 Prompt/Response、错误类型、来源与审核状态 |

负反馈及其 Candidate 在同一个事务中写入。`pending`、`accepted`、`rejected` 是审核状态，不是 Baseline 状态；这两张表中的任何记录都不能自动覆盖评估 Baseline。

## 定时任务

`cron_jobs` 保存 Schedule、Payload、Next/Last Execution 和 Enabled；`cron_job_runs` 记录每次 Scheduler Attempt 及结果 Message/Trace。

不要手工修改表来改变产品状态。诊断前备份数据库，Permission 与 Memory 治理使用 API。
