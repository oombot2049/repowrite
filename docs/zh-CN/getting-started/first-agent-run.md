# 第一次 Agent Run

[中文文档](../README.md) · [English](../../getting-started/first-agent-run.md)

这个过程验证完整本地链路：选择 Project、创建 Thread、模型流式响应、工具策略、持久化和最终回答。

## 1. 启动服务

```bash
uv run nexa serve --port 4096
```

打开 [http://127.0.0.1:4096](http://127.0.0.1:4096)。

## 2. 添加 Project

选择一个已有本地仓库。Project 是仓库的持久化身份和根目录边界，并不会复制源码。NexaPilot 将 Project 元数据保存到 SQLite，并将所选目录作为默认工作边界。

## 3. 创建 Thread

Thread 是一个 Project 内可持续进行的对话，持有 Message，并可以包含多个 Run。无关目标应使用不同 Thread，避免模型接收无关历史。

## 4. 发送边界明确的任务

先尝试只读请求：

```text
检查这个仓库，找出主要入口，并说明如何运行测试。不要修改文件。
```

控制台创建 Run 并流式显示时间线。最终回答前可能出现模型 Part 和读取、搜索类工具操作。

## 5. 验证审批

保持权限模式为 `ask`，发送一个小型写入任务：

```text
创建 nexapilot-smoke-test.txt，内容为“NexaPilot is ready”。
```

写工具申请权限时先检查参数。同意后执行；拒绝后系统记录被拒绝的 Tool Result，并将控制权交回模型。模型可以解释失败或选择安全替代方案。

## 6. 验证持久化

刷新页面或重启服务，Project、Thread、Message、Run、Tool Operation 和最终回答应从 SQLite 恢复。遗留的活跃 Run 会被保守协调，不会自动重放副作用不确定的工具。

## CLI 方式

```bash
uv run nexa run "总结这个仓库" --permission ask
```

任务一直处于 queued 或 Provider 没有返回首个事件时，先执行 `doctor`。
