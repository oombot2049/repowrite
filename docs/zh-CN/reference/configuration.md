# 配置参考

[中文文档](../README.md) · [English](../../reference/configuration.md) · [示例](../../../config.yaml.example)

配置格式为 YAML。先读取 `~/.nexa/config.yaml` 用户默认值，再由 `./.nexa/config.yaml` 覆盖。

## 必填 Provider 字段

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `openai.base_url` | URL | OpenAI-compatible API Root |
| `openai.api_key` | 非空字符串 | Provider 凭证，属于 Secret |
| `openai.model` | Model ID | 默认模型 |
| `openai.transport` | `auto`、`chat_completions`、`responses` | 协议 Adapter |
| `openai.reasoning_effort` | `none`、`low`、`medium`、`high`、`xhigh`、`max` | 请求的推理强度 |
| `openai.capability_profile` | `auto`、`openai`、`openai_compatible` | 能力解析 Profile |

`auto` 对 `api.openai.com` 使用 OpenAI Profile，其他 Host 保守使用 OpenAI-compatible Profile。

## Provider 弹性

`openai.resilience` 包含：

- `retry`：最大尝试次数和指数退避范围；
- `timeout`：Connect、First Event、Idle Stream、Total Attempt 超时；
- `circuit_breaker`：阈值、失败窗口与 Cooldown；
- `fallback.same_model_transport`：安全条件下允许同模型 Transport 回退；
- `fallback.models`：已废弃，请使用 `model_gateway.routes.*.candidates`。

`openai.budgets` 限制每个 Run 的 Call、Attempt、输入输出 Token 和估算微美元。未知价格保持 Unknown，不会按零处理。

## 多模型网关

`model_gateway` 是默认关闭的可选能力。`providers` 定义 Endpoint 和环境变量凭证引用，`models` 定义可部署模型的能力与价格，`routes` 定义候选顺序、安全错误分类、最大降级跳数和整条链共享的 Attempt 预算。完整语义见[多模型路由与降级机制](../architecture/model-routing-and-fallback.md)，可直接参考[配置样例](../../examples/model-gateway.yaml)。

## Runtime 与存储

| 字段 | 默认值/取值 | 含义 |
| --- | --- | --- |
| `db_path` | `./data/nexa.sqlite3` | 主 SQLite 数据库 |
| `default_worktree` | 未配置时自动探测 | 默认仓库根目录 |
| `default_permission_action` | 默认 `ask`，另有 `allow`、`deny` | 新 Session 回退 Policy |
| `local_guarded.enabled` | `true` | Host Shell 兼容保护 |
| `local_guarded.require_isolated_shell` | `false` | 强制隔离 Executor |
| `local_guarded.timeout_ms` | `120000` | 默认命令超时 |
| `local_guarded.max_timeout_ms` | `600000` | 最大允许超时 |
| `local_guarded.max_output_bytes` | `2000000` | 捕获输出上限 |

`durable_run` 配置心跳、Lease 和最大 Attempt。启动协调不会盲目重放副作用不确定的 Tool。

## 日志、Memory 与集成

`logging` 控制 Level、Console/File、目录、Rotation、Retention；`langfuse` 控制可选 Trace。Memory 的 Processing、Episodic、Semantic、Core、Context Manager 分别有独立开关。

可选段包括 `channels.feishu`、`kb`、`vlm`、`web_search`、`daytona`、`hooks` 和 `prompt_templates`。

公开配置 API 会脱敏 Secret。完整默认值和校验逻辑以 `config.yaml.example`、`src/nexapilot/config.py`、`src/nexapilot/config_schema.py` 为准。
