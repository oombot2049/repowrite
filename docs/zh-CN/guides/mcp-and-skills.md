# MCP 与 Skills 使用指南

[中文文档](../README.md) · [English](../../guides/mcp-and-skills.md)

MCP 与 Skill 扩展 NexaPilot 的不同层面：

- **MCP Server** 通过本地进程或网络 Transport 提供可执行工具；
- **Skill** 提供指令和配套资源，教 Agent 如何完成某类工作。

Skill 可以指导模型何时调用工具，但不能绕过 Tool Policy 或 Permission。

## MCP 操作

```bash
nexa mcp status
nexa mcp tools
nexa mcp connect <server-name>
nexa mcp disconnect <server-name>
```

添加本地 stdio Server：

```bash
nexa mcp add \
  --name local-fs \
  --command npx \
  --args "@modelcontextprotocol/server-filesystem,/safe/root"
```

添加配置不等于 Server 永远在线。启动或显式连接时，NexaPilot 启动/连接 Transport、发现 Tool Schema 并注册工具；只有模型之后调用工具时才发生具体外部动作。

## 信任边界

本地 stdio MCP Server 是运行在本机的独立进程，安装或启动它意味着执行第三方代码。必须检查 Package、Command、Argument、Environment、允许目录和网络行为。

MCP Tool 与内置 Tool 使用相同 Policy Engine，应按副作用配置 `allow`、`ask`、`deny`。工具发现不代表用户已经同意每次调用。

## Skill 操作

```bash
nexa skills list --worktree /path/to/repository
nexa skills get <skill-name>
```

Skill 加载与 Worktree 有关。Skill 应定义明确 Trigger、完整流程、所需资源、安全约束和失败降级。可复用 Skill 中不能包含凭证或私有仓库数据。

## 排障

- Server 未连接：检查状态、命令、stderr、URL 和认证。
- Tool 缺失：确认连接和 Schema Discovery，再检查 Agent Tool Allowlist。
- Tool 被拒绝：检查 Policy；MCP 不会绕过它。
- Skill 重复：清理多个 Discovery Root 中的同名定义。
