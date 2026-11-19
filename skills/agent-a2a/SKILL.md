---
name: agent-a2a
description: Use the installed local A2A bridge to send work from Orca or Codex to TRAE Desktop or Gemini CLI, inspect the link, or verify a workspace switch.
---

<!-- Installed by trae-gemini-a2a. -->

# Local A2A bridge (v2.1)

Use `a2a.cmd` from the project directory. In an already-running Orca process, use `& (Join-Path $env:LOCALAPPDATA 'AgentA2A\bin\a2a.cmd')` if the command is absent from PATH. For a custom installation, use its configured absolute launcher path.

- Run `a2a.cmd use` from the current project's root when assigning work that depends on that project. `a2a.cmd use --workspace <path>` explicitly selects the project. `a2a.cmd status` reports the active workspace and endpoint availability; use it only to inspect the link, never to poll task state.
- Dispatch to TRAE once: `a2a.cmd send --to trae --text "<task card>"`. It returns `{task_id, state}` immediately. Keep the `task_id`; do not re-dispatch the same task.
- Wait once for the terminal notification: `a2a.cmd wait --task-id <task_id> --timeout 620`. The local bridge tracks state, retries, the 420-second lease budget and the 600-second hard deadline. Do not run `a2a.cmd status`, `tasks/get`, or another `send` while waiting.
- The wait returns one card: `task_id`, `status` (`success` | `partial` | `timeout` | `failed`), `summary`, `card_path`, `event_id`, and `text`.
  - `success`: read the result card once and accept it.
  - `partial`: accept the completed part; read `summary` and `card_path` for the remaining items and open a follow-up task if needed.
  - `timeout`: no worker claimed the task before the hard deadline. Do not wait on the same `task_id` again; re-dispatch only if the work is still required.
  - `failed`: report the failure and do not retry unless the user asks.
- Deduplicate by `event_id`: the same task and result always produce the same `event_id`, including after reconnect or a repeated `wait`. If that event was already accepted, do nothing.
- The result card is `%LOCALAPPDATA%\AgentA2A\results\<taskId>.json` with schema `trae.a2a.result_card.v2.1` (`taskId`, `status`, `summary`, `text`, `workspace`, `createdAt`).
- `a2a.cmd send --to gemini --text "<task>"` keeps the synchronous Gemini reply. `a2a.cmd send --to trae --text "<task>" --wait` preserves the legacy blocking behavior.
- For Gemini CLI's native TRAE subagent, start `a2a.cmd gemini` in the project and invoke `trae-bridge`. For an inbound TRAE task, use `/trae-gemini-bridge` in the TRAE window for the selected project so it can claim and return the result.
