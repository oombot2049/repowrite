# Tool、Policy 与 Permission

[中文文档](../README.md) · [English](../../concepts/tools-policy-permission.md)

NexaPilot 将“系统能做什么”“系统策略是否允许”“用户是否同意”严格分开。

## Tool

Tool 是通过 JSON Schema 暴露给模型的可执行能力，例如读文件、搜索文本、写文件、运行命令、获取网页、搜索互联网、查询 Memory 或委派任务。

模型只负责提出 Tool 名称和参数。在 Schema 校验与策略检查通过前，这些参数都是不可信输入。

## Policy

Policy 是代码根据有序 Permission Rule 得出的确定性决策。规则匹配权限类别和参数 Pattern，返回：

- `allow`：无需询问用户，直接执行；
- `ask`：创建 Permission Request，暂停该操作；
- `deny`：不执行。

Policy 由代码而不是模型决定。Session Rule、Agent Profile 和继承的 deny 共同形成有效决策。

## Permission

Permission 是用户对 `ask` 决策的回答。同意后继续当前请求；拒绝后生成被拒绝的 Tool Result。模型会在历史中读到结果，然后解释、调整方案或选择其他工具。

因此审批不是第二个 Policy Engine，而是在 Policy 明确升级时解决一条待处理操作。

## Local Guard

Host Shell 的 Local Guard 会限制超时和最大输出等执行参数。它是最后一层兼容性保护，不是操作系统 Sandbox，也不能替代 Permission Rule。

```text
模型提出 Tool Call
       ↓
Schema 与参数校验
       ↓
Policy → allow / ask / deny
       ↓
ask 时由用户决策
       ↓
Executor Guard 与工具执行
       ↓
持久化 Tool Result 返回模型
```

拒绝和失败因此是可观察的普通结果，而不是静默终止 Agent Loop 的异常。实现参见[工具执行](../architecture/tool-execution.md)。
