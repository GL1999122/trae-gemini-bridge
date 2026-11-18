# TRAE Gemini A2A Bridge

在 Windows 上通过 A2A 0.3.0 在 TRAE 个人版桌面、Gemini CLI 和 Orca 终端之间派发文本任务。安装一次后可在不同项目中使用同一条 `a2a` 命令、同一个 TRAE 全局 Skill 和同一个 Gemini 远程子代理。

## 安装

需要 Python 3.10+、Node.js 24 LTS（含 npm）、TRAE CN 桌面版和 Gemini API Key。Gemini 官方服务可读取已有的 Gemini CLI API Key 登录，也可继承 `GEMINI_API_KEY` 环境变量。联网安装 npm 依赖后，运行 PowerShell 安装器。

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

如果默认模型的配额不可用，安装时传入自己有权使用的 Gemini 模型。

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -GeminiModel gemini-3.5-flash-lite
```

安装器将程序和随机生成的本机令牌放在 `%LOCALAPPDATA%\AgentA2A`，并挂载 TRAE 用户级 MCP 和 Skill、Gemini 用户级 `trae-bridge` 远程代理及 Orca/Codex 用户级 `agent-a2a` Skill。已有 TRAE 窗口可重载以读取更新。在 TRAE 设置的全局 MCP 列表确认 `agent-a2a-bridge` 已启用。

## 从 Orca 为当前项目使用

在该项目的 Orca 终端运行 `a2a use`。若已打开的 Orca 终端还未继承新的用户 PATH，使用下面的绝对路径。切换项目时，在新项目终端再次运行 `a2a use`。

```powershell
a2a use
a2a status
a2a send --to gemini --text '计算 3278 + 2865，只返回数字'
a2a send --to trae --text '读取当前项目 README.md 的第一行，原样返回'
a2a gemini
```

```powershell
& "$env:LOCALAPPDATA\AgentA2A\bin\a2a.cmd" use
```

可在 Orca 的 Quick Commands 中将这条命令保存为全局快捷入口。`a2a status` 显示选中的工作区和服务状态，`a2a stop` 关闭本机桥接服务。服务按需启动。

## TRAE 桌面与 Gemini CLI

1. 在 TRAE 打开与 `a2a use` 相同的项目，选择 `/trae-gemini-bridge`。可以让它调用 Gemini，也可以开始领取待办任务。
2. 若需要从 Gemini 派单，在该项目终端运行 `a2a gemini`，指示它调用 `trae-bridge` 子代理。包装命令运行原生 Gemini CLI，并为本次工作区连接设置凭证。
3. 在 TRAE 领取并执行任务。结果以 A2A 任务状态和结果文本回到发送方。

一台本机桥接服务同时绑定一个活动工作区。切换项目会启动针对新目录的官方 Gemini 服务。TRAE 领取时校验工作区，旧 Gemini 会话的凭证在切换后失效。TRAE 由桌面会话主动领取，队列只保存在本机进程内。

## 验证

安装依赖后，在仓库根目录运行回归测试。

```powershell
python -B -m unittest discover -s tests -v
```

## 上游实现

- [Gemini CLI 官方 A2A Server](https://github.com/google-gemini/gemini-cli/tree/v0.68.0/packages/a2a-server) 和 [Gemini CLI 远程子代理](https://github.com/google-gemini/gemini-cli/blob/main/docs/core/remote-agents.md)
- [A2A 0.3.0 规范](https://a2a-protocol.org/v0.3.0/specification/) 和 [A2A JavaScript SDK](https://github.com/a2aproject/a2a-js)
- [TRAE MCP](https://docs.trae.cn/ide_add-mcp-servers)、[TRAE Skill](https://docs.trae.cn/ide_skills) 和 [Agent Skills](https://github.com/agentskills/agentskills)
- [Orca 终端和全局 Quick Commands](https://api.onorca.dev/docs/terminal)
