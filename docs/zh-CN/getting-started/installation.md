# 安装

[中文文档](../README.md) · [English](../../getting-started/installation.md)

## 环境要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- NexaPilot 可以读取和修改的本地代码仓库
- OpenAI-compatible 模型服务与 API Key

## 从源码安装

```bash
git clone https://github.com/oombot2049/repowrite.git
cd repowrite
uv sync
```

`uv sync` 会根据锁文件创建虚拟环境并安装运行和开发依赖。

## 创建本地配置

NexaPilot 从 `~/.nexa/config.yaml` 读取用户默认值，从 `./.nexa/config.yaml` 读取项目覆盖值。第一次运行建议创建项目配置：

```bash
mkdir .nexa
cp config.yaml.example .nexa/config.yaml
```

PowerShell：

```powershell
New-Item -ItemType Directory -Force .nexa
Copy-Item config.yaml.example .nexa/config.yaml
```

`.nexa/` 已被 Git 忽略。API Key 和运行数据应保存在这里，禁止提交到仓库。

## 验证安装

完成[配置](configuration.md)后运行：

```bash
uv run nexa doctor
```

该命令检查配置加载、Provider 连通性、SQLite、MCP、Skills、知识库状态和有效 Worktree。长任务开始前应先修复失败项。

## 启动控制台

```bash
uv run nexa serve --port 4096
```

打开 [http://127.0.0.1:4096](http://127.0.0.1:4096)，继续完成[第一次 Agent Run](first-agent-run.md)。
