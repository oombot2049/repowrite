# CLI 使用指南

[中文文档](../README.md) · [English](../../guides/cli.md)

多数命令调用正在运行的 REST 服务；`serve`、`stop` 和 `doctor` 的部分检查也负责本地进程管理或离线诊断。

## 全局参数

```text
nexapilot [--json] [--base-url URL] [--version] [--help]
```

- `--base-url` 默认是 `http://127.0.0.1:4096`，也可读取 `NEXA_URL`。
- `--json` 必须放在子命令前；数据写 stdout，状态和错误写 stderr。

从源码目录运行时，在命令前添加 `uv run`。

## 最小工作流

```bash
uv run nexa doctor
uv run nexa serve --port 4096
uv run nexa run "解释主要模块" --permission ask
```

显式控制 Session：

```bash
nexa sessions create --worktree /path/to/project --title "架构分析"
nexa agent send <session-id> "梳理请求链路"
nexa agent run <session-id>
nexa sessions messages <session-id>
nexa agent interrupt <session-id>
```

## 命令组

| 命令组 | 用途 |
| --- | --- |
| `config` | 查看合并后的脱敏配置、来源、Schema 或初始化 YAML |
| `sessions` / `agent` | 管理对话，发送、运行和中断 Agent |
| `permissions` | 查询待审批请求并提交决定 |
| `logs` | 查询日志文件和结构化记录 |
| `cronjobs` | 管理定时唤醒任务 |
| `skills` | 查询 Worktree 下生效的 Skills |
| `mcp` | 查看工具并连接、断开或添加 MCP Server |
| `kb` | 操作可选知识库适配器 |
| `memory eval` | 执行离线 Memory 检索评估 |
| `eval` | 校验或执行 Agent 端到端 Dataset |

准确命令参数以 `nexapilot <group> --help` 和 `nexapilot <group> <command> --help` 为准，它们由当前安装版本直接生成。

## 自动化

```bash
nexapilot --json doctor
nexapilot --json sessions list
nexapilot --json config show
```

自动化脚本不要解析 Rich 终端文本，应使用 JSON 并将非零退出码视为失败。

## 常见问题

- 无法连接服务：启动 `serve` 或修正 `--base-url`。
- 首次配置无法由服务生成：先复制 `config.yaml.example` 再启动。
- 任务一直 queued：执行 `doctor`、查看日志和 Provider 状态。
- 自动化被审批阻塞：只在隔离 Fixture 中使用明确测试权限，不要对真实仓库全局关闭审批。
