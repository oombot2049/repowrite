# Project、Thread 与 Run

[中文文档](../README.md) · [English](../../concepts/projects-threads-runs.md)

NexaPilot 将仓库身份、对话历史和执行尝试拆开，使三者拥有正确的生命周期。

```text
Project
└── Thread（持久化层称 Session）
    ├── Message 与 Part
    ├── Run 1
    └── Run 2
        ├── Step
        ├── Tool Operation
        ├── Provider Attempt
        └── Artifact
```

## Project

Project 标识一个本地仓库根目录，用于聚合 Thread，并定义默认文件系统边界。添加 Project 不会上传或复制仓库内容。

界面统一使用 **Project**。部分内部类型和 API 使用 **worktree** 或 **cwd** 表示某次 Run 或 Tool 的执行位置，它们不是第二套项目实体。

## Thread

Thread 是 Project 内可持续进行的对话，持久化层称为 `session`。它持有有序的 User、Assistant、Tool 历史，以及 Goal、Todo 和权限规则。

目标或相关上下文明显变化时应新建 Thread。即使中间使用了其他 Thread，再次回到原 Thread 仍会保留之前的完整历史。

## Run

Run 是由一次用户输入或定时唤醒触发的执行尝试。一个 Run 可以包含多次模型调用和多批工具。Agent 先询问澄清问题、用户稍后回答时，通常会在同一 Thread 中创建下一个 Run。

Run 是状态、取消、心跳、预算、Provider Attempt、Artifact 和评估的操作边界，不等于一次 HTTP 请求或一次模型调用。

## Step 与 Tool Operation

Step 记录 Run 内有意义的执行单元，例如模型阶段或工具批次；Tool Operation 记录一次具体调用的参数、审批状态、输出、错误和耗时。

- Thread：用户和 Agent 讨论了什么？
- Run：这次触发导致了什么？
- Step：Run 执行到哪里？
- Tool Operation：发生了什么外部观察或副作用？

控制流参见 [Agent Loop](../architecture/agent-loop.md)，持久化参见[数据库 Schema](../reference/database-schema.md)。
