# 配置

[中文文档](../README.md) · [English](../../getting-started/configuration.md) · [完整参考](../reference/configuration.md)

NexaPilot 按以下顺序合并两份 YAML：

1. `~/.nexa/config.yaml`：用户级默认值
2. `./.nexa/config.yaml`：项目级覆盖值

项目配置优先。这样同一台机器可以共享 Provider 默认值，同时让每个 Checkout 使用独立数据库、日志、权限和 Feature Flag。

## 最小 Provider 配置

```yaml
openai:
  base_url: "https://api.openai.com/v1"
  api_key: "REPLACE_ME"
  model: "YOUR_MODEL"
  transport: "responses"
  reasoning_effort: "medium"

db_path: "./data/nexa.sqlite3"
default_permission_action: "ask"
```

支持 Responses 的 OpenAI 模型优先使用 `responses`；只实现 `/v1/chat/completions` 的兼容服务使用 `chat_completions`；`auto` 允许 Provider Gateway 自动选择，并在配置允许时回退 Transport。

## 安全的本地默认值

- 在理解全部工具前保留 `default_permission_action: ask`。
- 将运行数据放在 Git 忽略目录。
- 不要把本地服务暴露给不可信网络；当前产品假设本地操作者可信，不提供租户鉴权。
- 理解凭证与失败行为后再开启可选集成。

## Feature Flag

Memory 包含基础本地文件层和可独立控制的生产 Projection。示例配置默认关闭 Processing、Episodic、Semantic、Core 和 Context Manager。启用后使用 `/memory/status` 验证状态。

Provider 弹性、Durable Run、Local Guard、日志、Langfuse、飞书、知识库、Tavily 和 Daytona 都有独立配置段。准确字段与默认值参见[配置参考](../reference/configuration.md)。

## 验证有效配置

```bash
uv run nexa doctor
uv run nexa config show
```

Web 配置 API 会脱敏 Secret 字段。不要将本地原始 YAML 直接复制到 Issue。
