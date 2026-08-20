# Agent 评估指南

[中文文档](../README.md) · [English](../../guides/evaluation.md) · [示例](../../examples/README.zh-CN.md)

Agent 质量不只是最后一句回答。NexaPilot 同时评估对话和环境结果：文件、测试、Git Diff、工具轨迹、安全边界、延迟、Token 和估算成本。

## 校验 Dataset

```bash
nexa eval validate --dataset docs/examples/agent-eval.json
```

该命令检查 Schema、Fixture 路径、Checker 和 Budget，不运行 Agent。

## 执行评估

```bash
nexa eval run \
  --dataset docs/examples/agent-eval.json \
  --output-dir eval-results
```

每个 Case 都会将 Fixture 复制到隔离 Workspace，创建全新 Git Baseline，通过控制台相同的 Session/Message/Run API 调用 Agent，收集证据并执行确定性 Checker。

## 应该检查什么

- Run 是否进入预期终态；
- 最终回答是否包含或排除指定内容；
- 文件是否存在并具有预期内容；
- 测试命令是否得到预期退出码；
- Changed Files 是否保持在 Allowlist 内；
- 必需工具是否调用、禁止工具是否未调用；
- Tool Operation 是否出现异常错误；
- 是否生成预期 Artifact；
- 模型/工具调用、耗时、Token 和成本是否在预算内。

关键正确性和安全规则应该设置 Hard Gate。加权分数适合柔性质量维度，但不能掩盖越界修改文件。

## Baseline 回归

```bash
nexa eval run \
  --dataset docs/examples/agent-eval.json \
  --output-dir eval-results/current \
  --baseline eval-results/baseline/report.json
```

只有人工 Review 后才能晋升 Baseline，失败运行不能自动覆盖最近可信基线。

## 在线反馈与 Bad Case 审核

控制台会在每个已终止的 Assistant Run 下方显示不可变反馈入口。正反馈保存为满意度信号；负反馈至少要选择一个错误类型（`incorrect`、`incomplete`、`instruction_not_followed`、`tool_failure`、`unsafe`、`outdated` 或 `other`），并生成一个 `pending` Bad Case 候选。

这条写入链路刻意设置了人工门禁：

1. API 先定位 Canonical Run、触发它的 User Message 和对应 Assistant Message；
2. 对候选快照去除控制字符，并脱敏常见 Token、凭证、邮箱和用户目录；
3. Feedback 与可选 Candidate 在同一个 SQLite 事务中写入；相同请求重试是幂等的，第二份不同反馈会被拒绝，以保留清晰审计链；
4. 审核人在右侧 Observability 的 **Bad cases** 页面接受或拒绝候选；
5. 接受只表示进入“已审核 Bad Case 池”，不会自动生成 Checker，也绝不会把来源 Run 晋升为可信 Baseline。

这个边界对失败 Run 尤其重要：失败轨迹是有价值的回归输入，却不是“正确行为”的证据。维护者仍需把已接受候选整理成带 Fixture、Checks 和 Budget 的确定性 Eval Case，再单独进行 Baseline Review。

相关 API：

- `POST /runs/{run_id}/feedback`
- `GET /runs/{run_id}/feedback`
- `GET /evaluation/feedback`
- `GET /evaluation/candidates?status=pending`
- `POST /evaluation/candidates/{candidate_id}/review`

Feedback 表只保存脱敏后的反馈和候选快照；Canonical Messages 仍是事实来源，并继续遵循原有保留策略。

## 调查失败

`report.json` 保存机器可读证据，`report.md` 汇总 Review 信息。失败 Case 的 Workspace 会保留，便于检查文件和 Git Diff；使用 `--keep-workspaces` 保留成功 Case。

Dataset 应覆盖工具选择、拒绝审批、错误参数、工具超时、Provider 重试、取消、持久化恢复和 Memory 相关性，不能只有 Happy Path。
