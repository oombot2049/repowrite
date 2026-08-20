# 可执行示例

[中文文档](../zh-CN/README.md) · [English](README.md) · [Agent 评估指南](../zh-CN/guides/evaluation.md)

本目录保存机器可读的评估 Dataset 和 Fixture：

- `agent-eval.json`：个人 Agent 工具执行端到端评估集；
- `memory-eval.json`：Memory 检索与有效性评估集；
- `agent-eval-fixtures/`：每个 Case 使用的隔离仓库输入。

通过 NexaPilot CLI 校验和执行：

```bash
nexa eval validate --dataset docs/examples/agent-eval.json
nexa eval run --dataset docs/examples/agent-eval.json --output-dir eval-results
```

Fixture 必须确定、无 Secret，并且可以安全复制到临时 Workspace。失败 Case 会保留 Workspace，便于检查实际文件和 Git Diff。
