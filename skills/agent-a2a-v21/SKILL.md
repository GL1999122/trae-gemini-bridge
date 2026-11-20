---
name: agent-a2a-v21
description: Use the installed TRAE–Gemini V2.1 bridge when Codex or Orca should delegate to TRAE, send a task to Gemini, or check the active local workspace. TRAE receives work through the V2.1 MCP.
---

<!-- Installed by trae-gemini-a2a. -->

# TRAE–Gemini A2A V2.1

V2.1 uses a local, workspace-bound gateway. Submitting a task queues it without opening a TRAE conversation. A TRAE worker must claim it through MCP.

## Dispatch and receive

1. From the intended project root run `a2a.cmd use`. Use `a2a.cmd use --workspace <absolute path>` when selecting an explicit root. If PATH is stale, use `%LOCALAPPDATA%\AgentA2A\bin\a2a.cmd`. Use `a2a.cmd status` only to inspect the active workspace and service.
2. Run `a2a.cmd send --to trae --text "<task card>"` once. State the deliverable, allowed changes, and acceptance checks. Keep the returned `task_id`.
3. Run `a2a.cmd wait --task-id <task_id> --timeout 620` and let the local gateway handle reconnects and deadlines. Do not poll status or submit the same task while waiting. If the caller is interrupted, reconnect to the existing task before deciding whether another dispatch is needed.
4. Accept one terminal notification per `event_id`. Read the result once from `card_path` or the notification's `text`. The default card is `%LOCALAPPDATA%\AgentA2A\results\<taskId>.json`, schema `trae.a2a.result_card.v2.1`.

| Status | Next action |
| --- | --- |
| `success` | Verify the result against the task card. |
| `partial` | Accept the completed work. Check the remaining scope before making a new task. |
| `timeout` | Check worker availability. Dispatch again only if the work still needs doing. |
| `failed` | Report the failure and preserve the result. Retry only when requested. |

`a2a.cmd send --to gemini --text "<task>"` is synchronous. To delegate from a native Gemini CLI session, start `a2a.cmd gemini` and use its `trae-bridge` remote agent.

## Start a separate TRAE worker

Use a fresh `trae-cli exec` session when the CLI is installed and authenticated. Check `trae-cli login status` and use the project's root as the CLI working directory. Pass a per-session MCP configuration for the installed Python adapter `%LOCALAPPDATA%\AgentA2A\app\tools\trae_gemini_mcp.py --managed`. The adapter exposes MCP server version `2.1.0`. Keep authentication data out of prompts and task cards. If the CLI cannot authenticate, report the error. The TRAE Desktop option is a new conversation in the same project using the installed `/trae-gemini-bridge` worker skill.

Ask TRAE to call `claim_trae_task` with the exact absolute `workspace` and a short `wait_seconds`. TRAE uses the returned `taskId` and `leaseId` to call `complete_trae_task` once, with actual `text` and `status` (`done`, `partial`, or `failed`). The default execution budget is 420 seconds after claim within a 600-second hard deadline from dispatch. Include completed and remaining work in a partial result.
