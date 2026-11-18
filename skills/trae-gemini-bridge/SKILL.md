---
name: trae-gemini-bridge
description: 在 TRAE 桌面版用本机 A2A 向 Gemini 派单或领取发给 TRAE 的任务。用户要求与 Gemini、Codex 或 Orca 通过 A2A 协作时使用。
user-invocable: true
---

<!-- Installed by trae-gemini-a2a. -->

# TRAE 与 Gemini 通过 A2A 协作

使用用户级 `agent-a2a-bridge` MCP 提供的工具。

每次调用都使用当前 TRAE 窗口的实际工作区绝对路径作为 `workspace`。从 IDE 上下文确认路径。路径不明时先确认所在的工作区。

## 发给 Gemini

调用 `gemini_a2a_send`，传入 `workspace` 和独立的 `text` 任务。说明目标、操作范围及期望结果。回报工具返回的实际 `text` 和 `task_id`。调用失败时提供工具返回的错误。

## 领取给 TRAE 的任务

1. 调用 `claim_trae_task`，传入当前 `workspace` 和所需的 `wait_seconds`，每次最多等待 25 秒。没有任务时可再查两次。
2. 保留返回的 `taskId`、`leaseId`。在当前工作区及用户授权范围内执行任务。
3. 用 `complete_trae_task` 提交原始任务 ID、租约 ID 和实际结果。执行失败时设为 `failed: true` 并提交具体原因。
