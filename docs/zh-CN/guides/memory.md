# Memory 使用指南

[中文文档](../README.md) · [English](../../guides/memory.md) · [核心概念](../concepts/memory-and-context.md)

NexaPilot 可以组合本地 Markdown Memory 与可选的 Episodic、Semantic、Core 和 Context Manager Projection。生产 Projection 通过独立 Feature Flag 控制，便于逐项上线和观测。

## 启用 Pipeline

```yaml
memory:
  enabled: true
  processing:
    enabled: true
  episodic:
    enabled: true
  semantic:
    enabled: true
  core:
    enabled: true
    max_tokens: 1200
  context_manager:
    enabled: true
    shadow_mode: false
    max_input_tokens: 32000
    reserved_output_tokens: 4000
```

首次验证建议保留 `shadow_mode: true`，只观察选择结果而不改变线上 Prompt。确认日志、离线评估、相关性和 Token 使用符合预期后再关闭 Shadow Mode。

Embedding 是可选项。`embedding_base_url`、`embedding_api_key`、`embedding_model` 为空时仍可使用 FTS5/BM25 词法检索。

## 观察处理状态

Web API 提供：

- `GET /memory/status`：Feature Flag、Worker 与 Projection 状态；
- `GET /memory/outbox`：待处理或失败事件；
- `POST /memory/process`：请求处理；
- `GET /memory/episodes`、`/memory/semantic`：检查投影记录；
- `GET /memory/core`、`POST /memory/core/rebuild`：检查或重建 Core Memory。

Semantic Memory 可以通过治理 API 激活或遗忘。调查事实来源时必须保留并查看 Source ID。

## Run 中主动检索

`memory_search` 查找相关 Chunk 或 Projection，`memory_get` 获取精确内容。它们补充 Context Manager 的自动选择，而不是替代自动注入。

## 评估检索

```bash
nexa memory eval \
  --dataset docs/examples/memory-eval.json \
  --db-path ./data/nexa.sqlite3 \
  --min-recall 0.8
```

应同时关注 Recall、Prompt Token 成本、过期事实率和来源覆盖率。

## 运维原则

- Session Store 是对话事实来源。
- Projection Table 不能当作原始历史直接维护。
- 优先使用 Forget 和版本化，不破坏来源历史。
- Subagent 事实先保留为 Candidate，再通过可信路径激活。
- 手动治理前备份 SQLite。

内部实现参见 [Context 与 Memory 架构](../architecture/context-and-memory.md)。
