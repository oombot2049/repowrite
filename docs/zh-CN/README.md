# NexaPilot 中文文档

[项目首页](../../README.zh-CN.md) · [English](../README.md)

文档按照“你为什么打开它”组织：先从任务导向的指南开始，需要理解实现时再进入架构文档，需要准确字段和值时查询参考手册。

## 快速开始

- [安装](getting-started/installation.md)：安装依赖并准备本地配置。
- [配置](getting-started/configuration.md)：配置模型、存储、权限和可选能力。
- [第一次 Agent Run](getting-started/first-agent-run.md)：创建 Project 和 Thread，执行任务并检查结果。

## 核心概念

- [Project、Thread 与 Run](concepts/projects-threads-runs.md)
- [Tool、Policy 与 Permission](concepts/tools-policy-permission.md)
- [Memory 与 Context](concepts/memory-and-context.md)

概念文档解释对象是什么、为什么存在，不展开数据库字段和实现细节。

## 使用指南

- [CLI](guides/cli.md)
- [Memory](guides/memory.md)
- [Subagent](guides/subagents.md)
- [MCP 与 Skills](guides/mcp-and-skills.md)
- [Agent 评估](guides/evaluation.md)

使用指南说明如何配置、操作、验证和排查某项已实现能力。

## 架构设计

- [系统概览](architecture/overview.md)
- [Agent Loop](architecture/agent-loop.md)
- [Context 与 Memory](architecture/context-and-memory.md)
- [工具执行](architecture/tool-execution.md)
- [持久化与事件](architecture/persistence-and-events.md)
- [多模型路由与降级机制](architecture/model-routing-and-fallback.md)

架构文档描述运行时边界、控制流、失败语义和对应源码模块。

## 参考手册

- [配置项](reference/configuration.md)
- [数据库 Schema](reference/database-schema.md)
- [Provider 兼容性](reference/provider-compatibility.md)

参考手册用于查询准确字段和值。如果文档与实现不一致，以源码和测试为准并及时修正文档。

## 可执行示例

- [评估 Dataset 与 Fixture](../examples/README.zh-CN.md)

## 文档维护规则

- 只描述已实现行为，不把 Roadmap 当作当前能力。
- 用户可见行为变化时同步维护中英文版本。
- 每个概念只保留一个权威解释，其他位置使用链接。
- Guide 放操作过程与预期结果，Reference 放完整字段和值。
- 禁止写入 API Key、个人路径、私有仓库名称和运行时数据。
