---
name: agent-a2a
description: Use the installed local A2A bridge to send work from Orca or Codex to TRAE Desktop or Gemini CLI, inspect the link, or verify a workspace switch.
---

<!-- Installed by trae-gemini-a2a. -->

# Local A2A bridge

Use `a2a.cmd` from the project directory. In an already-running Orca process, use `& (Join-Path $env:LOCALAPPDATA 'AgentA2A\bin\a2a.cmd')` if the command is absent from PATH. For a custom installation, use its configured absolute launcher path.

- Run `a2a.cmd use` from the current project's root when assigning work that depends on that project. `a2a.cmd use --workspace <path>` explicitly selects the project. `a2a.cmd status` reports the active workspace and endpoint availability.
- Use `a2a.cmd send --to gemini --text "<task>"` or `a2a.cmd send --to trae --text "<task>"`. Include the scope, allowed operations, and expected output in the task. Report the returned task ID and actual state.
- For Gemini CLI's native TRAE subagent, start `a2a.cmd gemini` in the project and invoke `trae-bridge`. For an inbound TRAE task, use `/trae-gemini-bridge` in the TRAE window for the selected project so it can claim and return the result.
