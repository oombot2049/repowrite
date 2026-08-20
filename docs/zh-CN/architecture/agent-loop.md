# Agent Loop

[中文文档](../README.md) · [English](../../architecture/agent-loop.md)

NexaPilot 使用受控的 ReAct 风格循环：模型在决策、调用工具、观察持久化结果和生成最终回答之间迭代。Plan 和 Todo 可以指导循环，但不能替代循环。

## 控制流程

```python
lock(session_id)
load(session, message_history)
publish_status("busy")

while True:
    guard(turn_limit, interrupt_signal)
    context = build_context(session, history)
    tools = tool_registry.schemas()

    async for event in provider.stream(context, tools):
        persist_and_publish(event)
        collect_tool_calls(event)

    if no_tool_calls:
        finalize_assistant_message()
        break

    guard(tool_call_limit)
    execute_tool_batch_with_policy()
    history = reload_persisted_history()

publish_status("idle")
```

这是解释性伪代码，不是第二份实现；事实来源是 `SessionLoop`。

## 为什么重新加载 History

Provider 流式输出先写入 Message Part。Tool Call 随后创建 Tool Operation 和 Tool Result Message。重新加载确保下一次模型调用来自已提交事实，包括审批拒绝、超时、参数错误、输出截断和异常。

## Message 生命周期

Assistant Message 在流结束前创建；Text、Reasoning Metadata、Tool Call 到达时持续追加或更新 Part；Provider Turn 结束后 Message Finalize。这样实时 UI 与崩溃恢复共享同一事实，不需要反复覆盖一整块 JSON。

## 终止与安全

最终模型输出、显式 Interrupt、Cancel、Turn/Tool Limit、Provider Failure 或不可恢复异常都会结束循环。Durable Run State 用于区分真实终态和进程在 busy 时消失。

Interrupt Signal 在阶段之间检查。已产生外部副作用的 Tool 不能总是安全重放或回滚，因此启动协调优先标记保守的 interrupted，而不是自动重放不确定操作。

## Plan 与执行

Goal、Plan、Task、Todo 表达目标和进度，真正的模型/工具交互仍由 Agent Loop 完成。一个 Plan Task 可以触发 Run，一个 Run 可以调用多个工具，模型也可以在工作中更新 Todo。

Subagent 委派是普通 `task` Tool Call：Runtime 创建 Child Session 和 Child Run，再将有限 Child 输出作为 Tool Result 返回 Parent。
