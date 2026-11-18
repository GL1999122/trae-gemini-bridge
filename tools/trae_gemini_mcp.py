"""TRAE 本地 stdio MCP：通过 A2A 0.3 与 Gemini/TRAE 交换任务。

--managed 模式由安装器挂载，按 AGENT_A2A_HOME 使用本机桥接服务；
否则沿用 GEMINI_A2A_URL、TRAE_A2A_GATEWAY_URL 和 TRAE_A2A_RELAY_TOKEN
的独立模式。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_a2a as core  # noqa: E402


MAX_TEXT_LENGTH = core.MAX_TEXT_LENGTH
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH}
WORKSPACE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 4096}

COMPLETE_TOOL = {
    "name": "complete_trae_task",
    "description": "Return the actual result of the claimed A2A task to its caller. "
    "Set failed to true if the task could not be performed.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "lease_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "text": TEXT_SCHEMA,
            "failed": {"type": "boolean"},
        },
        "required": ["task_id", "lease_id", "text"],
        "additionalProperties": False,
    },
}


def send_tool(managed: bool) -> dict:
    properties = {
        "text": TEXT_SCHEMA,
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 110},
    }
    required = ["text"]
    if managed:
        properties = {"workspace": WORKSPACE_SCHEMA, **properties}
        required = ["workspace", "text"]
    return {
        "name": "gemini_a2a_send",
        "description": "Send a text task to the local Gemini A2A agent and return its reply.",
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
    }


def claim_tool(managed: bool) -> dict:
    properties = {"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 25}}
    required = []
    if managed:
        properties = {"workspace": WORKSPACE_SCHEMA, **properties}
        required = ["workspace"]
    return {
        "name": "claim_trae_task",
        "description": "Claim the next queued A2A task for this TRAE session. If one is returned, "
        "perform the task and call complete_trae_task with the same IDs and your actual result.",
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
    }


def get_text(arguments: dict, key: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise core.BridgeError(f"{key} 必须是非空文本且不超过 {maximum} 字符。")
    return value


def get_timeout(arguments: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise core.BridgeError(f"{key} 必须是 {minimum} 到 {maximum} 之间的整数。")
    return value


def get_workspace(arguments: dict) -> str:
    value = arguments.get("workspace")
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise core.BridgeError("workspace 必须是绝对路径文本。")
    if not os.path.isabs(value.strip()):
        raise core.BridgeError("workspace 必须是绝对路径。")
    try:
        workspace = core.canonical_workspace(value.strip())
    except core.BridgeError as error:
        raise core.BridgeError(f"workspace 无效：{error}") from error
    expected = os.environ.get("AGENT_A2A_TRAE_WORKSPACE", "").strip()
    if expected:
        expanded = os.path.expandvars(expected)
        if "${" not in expanded:
            try:
                expected_path = core.canonical_workspace(expanded)
            except core.BridgeError as error:
                raise core.BridgeError("AGENT_A2A_TRAE_WORKSPACE 不是有效绝对路径。") from error
            if not core.same_workspace(expected_path, workspace):
                raise core.BridgeError(
                    f"workspace 与 TRAE 当前工作区不一致：{workspace} != {expected_path}")
    return workspace


def active_health(workspace: str) -> dict:
    health = core.query_health()
    if health is None:
        raise core.BridgeError(
            f'桥接服务未启动。请先在该工作区运行 a2a use --workspace "{workspace}"。')
    if not core.same_workspace(str(health["workspace"]), workspace):
        raise core.BridgeError(
            f"当前活动工作区是 {health['workspace']}，与 {workspace} 不同；"
            f'请先运行 a2a use --workspace "{workspace}" 显式切换。')
    return health


def call_tool(params: dict, token: str, managed: bool) -> dict:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise core.BridgeError("工具参数必须是对象。")
    if name == "gemini_a2a_send":
        text = get_text(arguments, "text")
        timeout = get_timeout(arguments, "timeout_seconds", 90, 1, 110)
        if not managed:
            return core.send_to_gemini(text, timeout)
        workspace = get_workspace(arguments)
        return core.send_to_gemini_for_workspace(workspace, text, timeout, allow_start=True)
    if token and name == "claim_trae_task":
        wait_seconds = get_timeout(arguments, "wait_seconds", 0, 0, 25)
        if not managed:
            return core.claim_trae_task(wait_seconds, token)
        workspace = get_workspace(arguments)
        health = active_health(workspace)
        return core.claim_trae_task(wait_seconds, token, workspace=workspace,
                                    port=int(health["port"]))
    if token and name == "complete_trae_task":
        failed = arguments.get("failed", False)
        if not isinstance(failed, bool):
            raise core.BridgeError("failed 必须是布尔值。")
        return core.complete_trae_task(
            get_text(arguments, "task_id", 128), get_text(arguments, "lease_id", 128),
            get_text(arguments, "text"), failed, token=token)
    raise core.BridgeError("未知或未启用的工具。")


def handle(message: object, token: str, managed: bool, token_error: str = "") -> dict | None:
    if not isinstance(message, dict) or "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    method = message.get("method")
    params = message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        response["error"] = {"code": -32600, "message": "Invalid request."}
    elif method == "initialize":
        requested = params.get("protocolVersion")
        response["result"] = {
            "protocolVersion": requested if requested in MCP_VERSIONS else MCP_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "trae-gemini-a2a", "version": "1.0.0"},
        }
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        tools = ([send_tool(True), claim_tool(True), COMPLETE_TOOL] if managed
                 else ([send_tool(False), claim_tool(False), COMPLETE_TOOL] if token else [send_tool(False)]))
        response["result"] = {"tools": tools}
    elif method == "tools/call":
        try:
            if token_error:
                raise core.BridgeError(token_error)
            text = json.dumps(call_tool(params, token, managed), ensure_ascii=False)
            response["result"] = {"content": [{"type": "text", "text": text}]}
        except (core.BridgeError, OSError, ValueError, http.client.HTTPException) as error:
            response["result"] = {"content": [{"type": "text", "text": str(error)}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found."}
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--relay-token-file", type=Path)
    options = parser.parse_args()

    token = ""
    token_error = ""
    if options.managed:
        try:
            token = core.relay_token()
        except core.BridgeError as error:
            token_error = str(error)
    else:
        token = os.environ.get("TRAE_A2A_RELAY_TOKEN", "").strip()
        if not token and options.relay_token_file is not None:
            try:
                token = options.relay_token_file.read_text(encoding="utf-8-sig").strip()
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError):
                parser.error("Unable to read the relay token file.")
        if any(ord(character) < 33 or ord(character) > 126 for character in token):
            parser.error("The relay token must contain only visible ASCII characters.")

    for line in sys.stdin.buffer:
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}}
        else:
            response = handle(message, token, options.managed, token_error)
        if response is not None:
            sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
