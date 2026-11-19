"""跨工作区 TRAE ↔ Gemini A2A 桥接的命令行与共享客户端。

只访问 127.0.0.1 上的本机服务，不读取、输出或转发明文密钥。
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit


LOOPBACK = "127.0.0.1"
DEFAULT_RECEIVER_PORT = 18_768
DEFAULT_GEMINI_PORT = 41_243
AGENT_CARD_PATH = "/.well-known/agent-card.json"
MAX_TEXT_LENGTH = 16_384
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
START_TIMEOUT = 75.0
STOP_TIMEOUT = 20.0
CONTROL_LOCK_TIMEOUT = 20.0


class BridgeError(Exception):
    pass


def canonical_workspace(value: str | os.PathLike[str]) -> str:
    """返回保留大小写的真实绝对路径；短路径、junction 和链接会归一。"""
    text = os.path.expandvars(os.path.expanduser(os.fspath(value)))
    if not text or "\x00" in text:
        raise BridgeError("工作区路径为空或包含无效字符。")
    path = os.path.abspath(text)
    if not os.path.isabs(path):
        raise BridgeError("工作区必须是绝对路径。")
    return os.path.normpath(os.path.realpath(path))


def same_workspace(left: str, right: str) -> bool:
    def normalized(value: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(value)))

    return normalized(left) == normalized(right)


def agent_home() -> Path:
    override = os.environ.get("AGENT_A2A_HOME")
    if override:
        return Path(canonical_workspace(override))
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(os.path.normpath(os.path.join(local, "AgentA2A")))


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def receiver_port() -> int:
    return _port_from_env("AGENT_A2A_RECEIVER_PORT", DEFAULT_RECEIVER_PORT)


def gemini_port() -> int:
    return _port_from_env("AGENT_A2A_GEMINI_PORT", DEFAULT_GEMINI_PORT)


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as error:
        raise BridgeError(f"{name} 不是有效端口。") from error
    if not 0 < port <= 65_535:
        raise BridgeError(f"{name} 超出端口范围。")
    return port


def relay_token() -> str:
    path = agent_home() / "relay-token"
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as error:
        raise BridgeError("未找到 relay-token，请先运行安装器。") from error
    except (OSError, UnicodeError) as error:
        raise BridgeError("无法读取 relay-token。") from error
    if len(token) < 16 or any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise BridgeError("relay-token 内容无效，请重新运行安装器。")
    return token


def runtime_config() -> dict:
    path = agent_home() / "config.json"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise BridgeError("未找到 config.json，请先运行安装器。") from error
    except (OSError, UnicodeError) as error:
        raise BridgeError("无法读取 config.json。") from error
    try:
        config = json.loads(raw)
    except ValueError as error:
        raise BridgeError("config.json 不是有效 JSON。") from error
    if not isinstance(config, dict):
        raise BridgeError("config.json 必须是 JSON 对象。")
    node = config.get("node")
    if not isinstance(node, str) or not node.strip() or not os.path.isabs(node.strip()):
        raise BridgeError("config.json 的 node 必须是 Node.js 可执行文件的绝对路径。")
    model = config.get("gemini_model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise BridgeError("config.json 的 gemini_model 必须是非空文本。")
    return {"node": os.path.normpath(node.strip()), "gemini_model": model.strip() if isinstance(model, str) else None}


@contextmanager
def control_lock():
    """用 msvcrt 交叉进程互斥，避免并发 use/stop 互相覆盖。"""
    home = agent_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "control.lock", "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + CONTROL_LOCK_TIMEOUT
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise BridgeError("另一个 a2a 命令正在切换服务，请稍后重试。") from error
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _request(path: str, *, method: str = "GET", body: object = None, bearer: str | None = None,
             timeout: float = 5.0, port: int | None = None,
             accept: str = "application/json") -> tuple[int, object]:
    target = receiver_port() if port is None else port
    headers = {"Accept": accept}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection(LOOPBACK, target, timeout=timeout)
    try:
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BridgeError("本机服务响应超过大小限制。")
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
    return response.status, payload


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((LOOPBACK, port)) == 0


def query_health(*, port: int | None = None, timeout: float = 0.8) -> dict | None:
    """返回活动服务信息；端口无人监听时返回 None，被未知服务占用时抛错。"""
    target = receiver_port() if port is None else port
    try:
        status, payload = _request("/_admin/health", bearer=relay_token(), timeout=timeout, port=target)
    except (ConnectionRefusedError, ConnectionResetError, socket.timeout, TimeoutError):
        return None
    except OSError as error:
        if getattr(error, "winerror", None) == 10061 or error.errno in (61, 111):
            return None
        raise BridgeError(f"无法连接 127.0.0.1:{target}：{error}") from error
    except http.client.HTTPException as error:
        raise BridgeError(f"端口 {target} 已被其他服务占用；不会结束未知进程。") from error
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("workspace"), str):
        return payload
    if status == 404:
        raise BridgeError(f"端口 {target} 已被其他服务占用；不会结束未知进程。")
    raise BridgeError(f"端口 {target} 上的服务鉴权失败或响应异常（HTTP {status}）。")


def log_tail(limit: int = 4_000) -> str:
    path = agent_home() / "gateway.log"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace").strip()


def _refuse_active(health: dict, action: str) -> None:
    tasks = health.get("tasks") if isinstance(health.get("tasks"), dict) else {}
    queued = int(tasks.get("queued") or 0)
    working = int(tasks.get("working") or 0)
    if queued or working:
        raise BridgeError(f"当前工作区仍有 {queued} 个排队、{working} 个执行中的 TRAE 任务，已拒绝{action}。")


def _stop_health(health: dict) -> None:
    port = health.get("port") if isinstance(health.get("port"), int) else receiver_port()
    status, payload = _request("/_admin/stop", method="POST", body={}, bearer=relay_token(), port=port)
    if status == 200:
        return
    if status == 409:
        tasks = payload if isinstance(payload, dict) else {}
        queued = tasks.get("queued", "?")
        working = tasks.get("working", "?")
        raise BridgeError(f"当前工作区仍有 {queued} 个排队、{working} 个执行中的 TRAE 任务，已拒绝停止。")
    if status == 401:
        raise BridgeError("relay-token 与本机服务不匹配，请重新运行安装器。")
    raise BridgeError(f"本机服务拒绝停止（HTTP {status}）。")


def _wait_stopped() -> None:
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not port_open(receiver_port()) and not port_open(gemini_port()):
            return
        time.sleep(0.1)
    raise BridgeError("桥接服务停止超时，请稍后重试 a2a stop。")


def _spawn_gateway(workspace: str, session: str) -> subprocess.Popen:
    config = runtime_config()
    node = Path(config["node"])
    if not node.is_file():
        raise BridgeError(f"config.json 指定的 Node.js 不存在：{node}")
    script = app_root() / "tools" / "trae_a2a_gateway.py"
    if not script.is_file():
        raise BridgeError(f"缺少网关脚本：{script}")
    home = agent_home()
    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "gateway.log"
    log_path.write_bytes(b"")
    environment = os.environ.copy()
    environment["AGENT_A2A_HOME"] = str(home)
    environment["AGENT_A2A_SESSION_TOKEN"] = session
    environment["PYTHONIOENCODING"] = "utf-8"
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    command = [sys.executable, "-B", str(script), "--workspace", workspace,
               "--port", str(receiver_port()), "--gemini-port", str(gemini_port())]
    for name, option in (("AGENT_A2A_TASK_TIMEOUT", "--task-timeout"),
                         ("AGENT_A2A_LEASE_BUDGET", "--lease-budget")):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                value = int(raw)
            except ValueError as error:
                raise BridgeError(f"{name} 不是有效整数。") from error
            command += [option, str(value)]
    with open(log_path, "ab", buffering=0) as log:
        return subprocess.Popen(
            command,
            cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
        )


def _kill_spawned(process: subprocess.Popen) -> None:
    """只清理本次刚启动的网关进程及其子进程。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.terminate()
        process.wait(10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _wait_ready(process: subprocess.Popen, workspace: str, session: str) -> dict:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BridgeError(f"桥接服务启动失败（退出码 {process.returncode}）。\n{log_tail()}")
        try:
            health = query_health(timeout=0.5)
        except BridgeError:
            _kill_spawned(process)
            raise
        if health is not None:
            if not same_workspace(str(health["workspace"]), workspace):
                _kill_spawned(process)
                raise BridgeError(f"端口被另一工作区的服务抢先占用：{health['workspace']}")
            if not hmac.compare_digest(str(health.get("sessionToken", "")), session):
                _kill_spawned(process)
                raise BridgeError("桥接服务返回的会话凭证与本次启动不一致。")
            state = (health.get("gemini") or {}).get("state")
            if state in {"running", "disabled"}:
                return health
            if state == "exited":
                _kill_spawned(process)
                raise BridgeError(f"官方 Gemini 服务已退出。\n{log_tail()}")
        time.sleep(0.1)
    _kill_spawned(process)
    raise BridgeError(f"桥接服务启动超时。\n{log_tail()}")


def _await_gemini(health: dict) -> dict:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = (health.get("gemini") or {}).get("state")
        if state in {"running", "disabled"}:
            return health
        if state == "exited":
            raise BridgeError(f"官方 Gemini 服务已退出。\n{log_tail()}")
        time.sleep(0.1)
        health = query_health(timeout=0.8)
        if health is None:
            raise BridgeError(f"桥接服务在启动 Gemini 时退出。\n{log_tail()}")
    raise BridgeError(f"等待官方 Gemini 服务就绪超时。\n{log_tail()}")


def _activate_locked(target: str, *, allow_switch: bool) -> tuple[dict, bool]:
    health = query_health()
    if health is not None:
        if same_workspace(health["workspace"], target):
            return _await_gemini(health), False
        if not allow_switch:
            raise BridgeError(
                f"当前活动工作区是 {health['workspace']}。请先运行 "
                f'a2a use --workspace "{target}" 显式切换；不会自动切换。'
            )
        _refuse_active(health, "切换")
        _stop_health(health)
        _wait_stopped()
    elif port_open(receiver_port()):
        raise BridgeError(f"端口 {receiver_port()} 已被其他服务占用；不会结束未知进程。")
    if port_open(gemini_port()):
        raise BridgeError(f"端口 {gemini_port()} 已被其他服务占用；不会结束未知进程。")
    session = secrets.token_urlsafe(32)
    process = _spawn_gateway(target, session)
    return _wait_ready(process, target, session), True


def activate(workspace: str, *, allow_switch: bool = True) -> tuple[dict, bool]:
    """确保目标工作区为唯一活动工作区；返回 (health, 是否新启动)。"""
    target = canonical_workspace(workspace)
    if not os.path.isdir(target):
        raise BridgeError(f"工作区不存在或不可访问：{target}")
    with control_lock():
        return _activate_locked(target, allow_switch=allow_switch)


def _require_active_locked(target: str) -> dict:
    health = query_health()
    if health is None:
        raise BridgeError(f'桥接服务未启动。请先运行 a2a use --workspace "{target}"。')
    if not same_workspace(health["workspace"], target):
        raise BridgeError(
            f"当前活动工作区是 {health['workspace']}，不是当前目录 {target}；"
            f'请先运行 a2a use --workspace "{target}"。'
        )
    return health


def require_active(workspace: str) -> dict:
    target = canonical_workspace(workspace)
    return _require_active_locked(target)


def send_to_gemini_for_workspace(workspace: str, text: str, timeout_seconds: int,
                                 *, allow_start: bool) -> dict:
    """校验活动工作区并完成 Gemini 调用；整个过程持有跨进程运行锁。"""
    target = canonical_workspace(workspace)
    if not os.path.isdir(target):
        raise BridgeError(f"工作区不存在或不可访问：{target}")
    with control_lock():
        if allow_start:
            health, _ = _activate_locked(target, allow_switch=False)
        else:
            health = _require_active_locked(target)
        state = (health.get("gemini") or {}).get("state")
        if state != "running":
            raise BridgeError(f"官方 Gemini 服务未就绪（{state}）。")
        return send_to_gemini(text, timeout_seconds,
                              url=f"http://{LOOPBACK}:{health['geminiPort']}/")


def ensure_service(workspace: str, *, allow_switch: bool) -> dict:
    return activate(workspace, allow_switch=allow_switch)[0]


def stop_service() -> bool:
    with control_lock():
        health = query_health()
        if health is None:
            if port_open(receiver_port()):
                raise BridgeError(f"端口 {receiver_port()} 被其他服务占用；不会结束未知进程。")
            return False
        _refuse_active(health, "停止")
        _stop_health(health)
        _wait_stopped()
        return True


def public_status(health: dict) -> dict:
    return {
        "active": True,
        "workspace": health.get("workspace"),
        "pid": health.get("pid"),
        "endpoint": f"http://{LOOPBACK}:{health.get('port')}/",
        "geminiPort": health.get("geminiPort"),
        "gemini": health.get("gemini"),
        "tasks": health.get("tasks"),
        "startedAt": health.get("startedAt"),
    }


# --------------------------------------------------------------------------
# A2A 0.3 客户端（共享 SSE / JSON-RPC / 租约逻辑）
# --------------------------------------------------------------------------


def local_endpoint(url: str) -> tuple[str, int, str]:
    address = urlsplit(url)
    host = address.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (address.scheme != "http" or not loopback or address.username is not None
            or address.password is not None or address.fragment or address.query):
        raise BridgeError("需要无凭证、无查询参数的本机 HTTP loopback URL。")
    return host, address.port or 80, address.path or "/"


@contextmanager
def http_response(endpoint: tuple[str, int, str], *, path: str | None = None, body: object = None,
                  timeout: float = 5, bearer: str | None = None, accept: str = "application/json"):
    host, port, default_path = endpoint
    headers = {"Accept": accept}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET" if body is None else "POST", path or default_path, data, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise BridgeError(f"本机 A2A 服务返回 HTTP {response.status}。")
        yield response
    finally:
        connection.close()


def http_json(endpoint: tuple[str, int, str], **kwargs) -> dict:
    with http_response(endpoint, **kwargs) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BridgeError("本机响应超过大小限制。")
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise BridgeError("本机服务返回了非 JSON 响应。") from error
    if not isinstance(payload, dict):
        raise BridgeError("本机服务返回了非对象 JSON 响应。")
    return payload


def rpc(endpoint: tuple[str, int, str], method: str, params: dict, deadline: float,
        *, bearer: str | None = None) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BridgeError("A2A 任务超时。")
    request_id = str(uuid.uuid4())
    payload = http_json(endpoint, body={
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
    }, timeout=remaining, bearer=bearer)
    return rpc_result(payload, request_id)


def rpc_result(payload: dict, request_id: str) -> dict:
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
        raise BridgeError("A2A 服务返回了不匹配的 JSON-RPC 响应。")
    if isinstance(payload.get("error"), dict):
        error = payload["error"]
        raise BridgeError(f"A2A 错误 {error.get('code')}: {error.get('message', '')}")
    if not isinstance(payload.get("result"), dict):
        raise BridgeError("A2A 服务未返回结果。")
    return payload["result"]


def parts_text(content: dict) -> str:
    return "".join(part["text"] for part in content.get("parts", [])
                   if isinstance(part, dict) and part.get("kind") == "text"
                   and isinstance(part.get("text"), str))


def reply_text(result: dict) -> str:
    if result.get("kind") == "message" and result.get("role") == "agent":
        return parts_text(result)
    status = result.get("status", {})
    message = status.get("message")
    if isinstance(message, dict) and parts_text(message):
        return parts_text(message)
    messages = [parts_text(entry) for entry in result.get("history", [])
                if isinstance(entry, dict) and entry.get("role") == "agent"]
    return "\n".join(filter(None, messages)) or "\n".join(
        text for artifact in result.get("artifacts", [])
        if isinstance(artifact, dict) and (text := parts_text(artifact)))


def record_tool_states(message: object, tool_states: dict) -> None:
    if not isinstance(message, dict) or message.get("role") != "agent":
        return
    for part in message.get("parts", []):
        data = part.get("data") if isinstance(part, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
            continue
        call_id = data["request"].get("callId")
        if isinstance(call_id, str):
            tool_states[call_id] = data.get("status")


def gemini_task_reply(task_id: str | None, state: str | None, text: str, tool_states: dict) -> dict:
    if state not in {"completed", "input-required", None} or "awaiting_approval" in tool_states.values():
        raise BridgeError(f"Gemini 任务 {task_id} 停在 {state}: {text}")
    if not text:
        raise BridgeError(f"Gemini 任务 {task_id} 没有返回文本。")
    return {"task_id": task_id, "state": state, "text": text}


def stream_events(endpoint: tuple[str, int, str], params: dict, deadline: float, *, bearer: str | None = None,
                  method: str = "message/stream"):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BridgeError("A2A 任务超时。")
    request_id = str(uuid.uuid4())
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    with http_response(endpoint, body=request, timeout=remaining, bearer=bearer,
                       accept="text/event-stream") as response:
        if response.getheader("Content-Type", "").lower().split(";", 1)[0] != "text/event-stream":
            raise BridgeError("A2A 服务未返回事件流。")
        total_bytes = 0
        data = []
        while line := response.readline(MAX_RESPONSE_BYTES + 1):
            total_bytes += len(line)
            if total_bytes > MAX_RESPONSE_BYTES:
                raise BridgeError("A2A 事件流超过大小限制。")
            if time.monotonic() > deadline:
                raise BridgeError("A2A 任务超时。")
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif not line and data:
                yield rpc_result(json.loads(b"\n".join(data)), request_id)
                data.clear()


def stream_gemini_reply(endpoint: tuple[str, int, str], params: dict, deadline: float,
                        *, bearer: str | None = None) -> dict:
    task_id = None
    chunks = {}
    tool_states = {}
    with closing(stream_events(endpoint, params, deadline, bearer=bearer)) as events:
        for event in events:
            match event.get("kind"):
                case "task":
                    task_id = event.get("id")
                    state = event.get("status", {}).get("state")
                    if state in {"completed", "input-required", "failed", "canceled", "rejected"}:
                        return gemini_task_reply(task_id, state, reply_text(event), tool_states)
                case "message":
                    return gemini_task_reply(event.get("taskId"), None, reply_text(event), tool_states)
                case "status-update":
                    task_id = event.get("taskId", task_id)
                    status = event.get("status", {})
                    message = status.get("message")
                    record_tool_states(message, tool_states)
                    source = event.get("metadata", {}).get("coderAgent", {}).get("kind")
                    if source == "text-content" and isinstance(message, dict):
                        chunks.setdefault(message.get("messageId"), []).append(parts_text(message))
                    if event.get("final"):
                        text = "\n".join("".join(parts) for parts in chunks.values())
                        if isinstance(message, dict) and (not text or status.get("state") == "failed"):
                            text = parts_text(message) or text
                        return gemini_task_reply(task_id, status.get("state"), text, tool_states)
    raise BridgeError(f"事件流在任务 {task_id} 完成前结束。")


def _agent_endpoint(card_url: str, timeout: float, *, bearer: str | None = None,
                    label: str = "A2A") -> tuple[dict, tuple[str, int, str]]:
    gateway = local_endpoint(card_url)
    card = http_json(gateway, path=AGENT_CARD_PATH, timeout=min(5, timeout), bearer=bearer)
    if card.get("protocolVersion") != "0.3.0":
        raise BridgeError(f"{label} Agent Card 未声明 A2A 0.3.0。")
    url = card.get("url") if card.get("preferredTransport", "JSONRPC") == "JSONRPC" else None
    if not url:
        url = next((item.get("url") for item in card.get("additionalInterfaces", [])
                    if item.get("transport") == "JSONRPC"), None)
    if not isinstance(url, str):
        raise BridgeError(f"{label} Agent Card 未声明 JSON-RPC 端点。")
    _, port, path = local_endpoint(url)
    if port != gateway[1]:
        raise BridgeError(f"{label} Agent Card 指向了不同的本机端口。")
    return card, (gateway[0], port, path)


def send_to_gemini(text: str, timeout_seconds: int, *, url: str | None = None,
                   bearer: str | None = None) -> dict:
    if url is None:
        url = os.environ.get("GEMINI_A2A_URL", f"http://{LOOPBACK}:{gemini_port()}/")
    deadline = time.monotonic() + timeout_seconds
    card, endpoint = _agent_endpoint(url, timeout_seconds, bearer=bearer, label="Gemini")
    params = {
        "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                    "parts": [{"kind": "text", "text": text}]},
        "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain"], "historyLength": 24},
    }
    if card.get("capabilities", {}).get("streaming") is True:
        return stream_gemini_reply(endpoint, params, deadline, bearer=bearer)
    result = rpc(endpoint, "message/send", params, deadline, bearer=bearer)
    state = None
    tool_states = {}
    task_id = None
    if result.get("kind") == "task":
        task_id = result.get("id")
        while result.get("status", {}).get("state") in {"submitted", "working"}:
            if not isinstance(task_id, str) or not task_id:
                raise BridgeError("Gemini 返回了没有 ID 的任务。")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            result = rpc(endpoint, "tasks/get", {"id": task_id, "historyLength": 24}, deadline, bearer=bearer)
        state = result.get("status", {}).get("state")
        for message in result.get("history", []):
            record_tool_states(message, tool_states)
    else:
        task_id = result.get("taskId")
    return gemini_task_reply(task_id, state, reply_text(result), tool_states)


def _trae_endpoint(health: dict, timeout_seconds: float) -> tuple[tuple[str, int, str], str]:
    port = health.get("port")
    session = health.get("sessionToken")
    if not isinstance(port, int) or not isinstance(session, str) or not session:
        raise BridgeError("活动服务未提供可用的 A2A 会话凭证。")
    base = f"http://{LOOPBACK}:{port}/"
    _, endpoint = _agent_endpoint(base, timeout_seconds, bearer=session, label="TRAE")
    return endpoint, session


def dispatch_to_trae(text: str, health: dict, *, attempts: int = 2) -> dict:
    """立即派单并返回 taskId；状态跟踪由本机网关承担，不在客户端轮询。"""
    deadline = time.monotonic() + 10
    endpoint, session = _trae_endpoint(health, 10)
    params = {
        "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                    "parts": [{"kind": "text", "text": text}]},
        "configuration": {"blocking": False, "acceptedOutputModes": ["text/plain"], "historyLength": 0},
    }
    for attempt in range(max(1, attempts)):
        try:
            result = rpc(endpoint, "message/send", params, deadline, bearer=session)
            break
        except (ConnectionError, socket.timeout, TimeoutError, http.client.HTTPException) as exc:
            if attempt + 1 >= attempts:
                raise BridgeError(f"派单失败：{exc}") from exc
            time.sleep(0.25 * (attempt + 1))
    task_id = result.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise BridgeError("TRAE 返回了没有 ID 的任务。")
    return {"task_id": task_id, "state": result.get("status", {}).get("state", "submitted")}


def terminal_card(task_id: str, event: dict, text: str) -> dict:
    status = event.get("status", {})
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    state = status.get("state")
    outcome = metadata.get("outcome") or ("success" if state == "completed" else "failed")
    summary = metadata.get("summary")
    if not summary:
        summary = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return {
        "task_id": task_id,
        "status": outcome,
        "state": state,
        "summary": (summary or "")[:160],
        "card_path": metadata.get("cardPath"),
        "event_id": metadata.get("eventId") or f"{task_id}:{outcome}",
        "text": text,
    }


def wait_for_trae_task(task_id: str, timeout_seconds: int, health: dict) -> dict:
    """在本机等待唯一一条终态通知；断线用 tasks/resubscribe 重连，不查询状态。"""
    deadline = time.monotonic() + timeout_seconds
    endpoint, session = _trae_endpoint(health, timeout_seconds)
    reconnects = 0
    text_chunks: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError(f"等待 TRAE 任务 {task_id} 终态超时。")
        try:
            with closing(stream_events(endpoint, {"id": task_id}, deadline, bearer=session,
                                       method="tasks/resubscribe")) as events:
                final = None
                snapshot = None
                for event in events:
                    kind = event.get("kind")
                    if kind == "artifact-update":
                        chunk = parts_text(event.get("artifact", {}))
                        if chunk:
                            text_chunks.append(chunk)
                    elif kind == "status-update":
                        if event.get("final"):
                            final = event
                            break
                    elif kind == "task":
                        snapshot = event
                if final is not None:
                    return terminal_card(task_id, final, "".join(text_chunks))
                if snapshot is not None and snapshot.get("status", {}).get("state") in {
                        "completed", "failed", "canceled"}:
                    return terminal_card(task_id, snapshot, "".join(text_chunks))
        except (ConnectionError, socket.timeout, TimeoutError, http.client.HTTPException):
            pass
        reconnects += 1
        if reconnects > 32:
            raise BridgeError(f"等待 TRAE 任务 {task_id} 终态时连续断线。")
        time.sleep(min(1.0, 0.2 * reconnects))


def send_to_trae(text: str, timeout_seconds: int, health: dict) -> dict:
    port = health.get("port")
    session = health.get("sessionToken")
    if not isinstance(port, int) or not isinstance(session, str) or not session:
        raise BridgeError("活动服务未提供可用的 A2A 会话凭证。")
    base = f"http://{LOOPBACK}:{port}/"
    deadline = time.monotonic() + timeout_seconds
    _, endpoint = _agent_endpoint(base, timeout_seconds, bearer=session, label="TRAE")
    params = {
        "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                    "parts": [{"kind": "text", "text": text}]},
        "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain"], "historyLength": 24},
    }
    result = rpc(endpoint, "message/send", params, deadline, bearer=session)
    task_id = result.get("id")
    while result.get("status", {}).get("state") in {"submitted", "working"}:
        if not isinstance(task_id, str) or not task_id:
            raise BridgeError("TRAE 返回了没有 ID 的任务。")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        result = rpc(endpoint, "tasks/get", {"id": task_id, "historyLength": 24}, deadline, bearer=session)
    state = result.get("status", {}).get("state")
    text_result = reply_text(result)
    if state != "completed":
        raise BridgeError(f"TRAE 任务 {task_id} 停在 {state}: {text_result}")
    if not text_result:
        raise BridgeError(f"TRAE 任务 {task_id} 没有返回文本。")
    return {"task_id": task_id, "state": state, "text": text_result}


def relay_request(path: str, body: dict, token: str, *, url: str | None = None,
                  port: int | None = None) -> dict:
    if url is not None:
        _, target_port, _ = local_endpoint(url)
        port = target_port
    status, payload = _request(path, method="POST", body=body, bearer=token, port=port)
    if status == 200 and isinstance(payload, dict):
        return payload
    message = payload.get("error") if isinstance(payload, dict) else None
    raise BridgeError(str(message) if message else f"本机桥接返回 HTTP {status}。")


def claim_trae_task(wait_seconds: int, token: str, *, workspace: str | None = None,
                    url: str | None = None, port: int | None = None) -> dict:
    deadline = time.monotonic() + wait_seconds
    body = {"workspace": workspace} if workspace is not None else {}
    while True:
        task = relay_request("/_relay/claim", body, token, url=url, port=port)
        if task:
            return task
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        time.sleep(min(0.5, remaining))


def complete_trae_task(task_id: str, lease_id: str, text: str, failed: bool, *, token: str,
                       status: str = "done", url: str | None = None, port: int | None = None) -> dict:
    if status not in {"done", "partial", "failed"}:
        raise BridgeError("status 必须是 done、partial 或 failed。")
    return relay_request(
        "/_relay/fail" if failed else "/_relay/complete",
        {"taskId": task_id, "leaseId": lease_id, "text": text, "status": status},
        token, url=url, port=port,
    )


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def gemini_command(arguments: list[str]) -> list[str]:
    root = app_root()
    config = runtime_config()
    loopback = root / "node" / "gemini-loopback.mjs"
    bundle = root / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
    for path in (loopback, bundle):
        if not path.is_file():
            raise BridgeError(f"缺少 Gemini CLI 文件：{path}")
    return [config["node"], "--import", loopback.as_uri(), str(bundle), *arguments]


def run_gemini(arguments: list[str]) -> int:
    workspace = canonical_workspace(os.getcwd())
    health, _ = activate(workspace, allow_switch=True)
    session = health.get("sessionToken")
    if not isinstance(session, str) or not session:
        raise BridgeError("活动服务未提供会话凭证。")
    environment = os.environ.copy()
    environment["AGENT_A2A_TRAE_ORIGIN"] = f"http://{LOOPBACK}:{health['port']}"
    environment["AGENT_A2A_SESSION_TOKEN"] = session
    return subprocess.call(gemini_command(arguments), cwd=workspace, env=environment)


def _readable(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a2a", description="本机跨工作区 TRAE ↔ Gemini A2A 桥接。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    use = subparsers.add_parser("use", help="启动或切换到指定工作区（默认当前目录）。")
    use.add_argument("--workspace")
    subparsers.add_parser("status", help="显示活动工作区和服务状态。")
    subparsers.add_parser("stop", help="停止本机桥接服务。")
    send = subparsers.add_parser("send", help="发送文本任务。")
    send.add_argument("--to", required=True, choices=("gemini", "trae"))
    send.add_argument("--text", required=True)
    send.add_argument("--timeout", type=int, default=110)
    send.add_argument("--wait", action="store_true",
                      help="向 TRAE 派单后阻塞到终态（兼容旧行为）；默认立即返回 taskId。")
    wait = subparsers.add_parser("wait", help="等待 TRAE 任务的一条终态通知（不查询状态）。")
    wait.add_argument("--task-id", required=True)
    wait.add_argument("--timeout", type=int, default=620)
    subparsers.add_parser("gemini", help="用当前工作区启动原生 Gemini CLI。", add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["gemini"]:
            return run_gemini(arguments[1:])
        parser = build_parser()
        options = parser.parse_args(arguments)
        if options.command == "use":
            health, started = activate(options.workspace or os.getcwd(), allow_switch=True)
            print(_readable({
                "active": True, "workspace": health["workspace"], "started": started,
                "endpoint": f"http://{LOOPBACK}:{health['port']}/",
                "geminiPort": health.get("geminiPort"),
                "gemini": (health.get("gemini") or {}).get("state"),
            }))
            return 0
        if options.command == "status":
            health = query_health()
            if health is None:
                if port_open(receiver_port()):
                    raise BridgeError(f"端口 {receiver_port()} 被其他服务占用；不会结束未知进程。")
                print(_readable({"active": False}))
            else:
                print(_readable(public_status(health)))
            return 0
        if options.command == "stop":
            print(_readable({"stopped": stop_service()}))
            return 0
        if options.command == "wait":
            if not 1 <= options.timeout <= 900:
                raise BridgeError("timeout 必须在 1 到 900 秒之间。")
            health = require_active(canonical_workspace(os.getcwd()))
            card = wait_for_trae_task(options.task_id.strip(), options.timeout, health)
            print(_readable({"to": "trae", **card}))
            return 0
        text = options.text
        if not text.strip() or len(text) > MAX_TEXT_LENGTH:
            raise BridgeError(f"text 必须是非空文本且不超过 {MAX_TEXT_LENGTH} 字符。")
        if not 1 <= options.timeout <= 900:
            raise BridgeError("timeout 必须在 1 到 900 秒之间。")
        if options.to == "gemini":
            result = send_to_gemini_for_workspace(
                canonical_workspace(os.getcwd()), text, options.timeout, allow_start=False)
        else:
            health = require_active(canonical_workspace(os.getcwd()))
            if not options.wait:
                print(_readable({"to": "trae", **dispatch_to_trae(text, health)}))
                return 0
            result = send_to_trae(text, options.timeout, health)
        print(_readable({"to": options.to, **result}))
        return 0
    except BridgeError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"错误：本机操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
