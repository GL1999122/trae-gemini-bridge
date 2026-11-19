"""把单个 TRAE 桌面工作区暴露为本机 A2A 0.3 agent。

网关进程绑定一个活动工作区，并作为官方 Gemini A2A child 的监督者：
Agent Card 公开可读，A2A POST 使用本次激活随机生成的会话 bearer，
私有健康/停止/领取接口只接受安装器生成的 relay token。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from uuid import uuid4


LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 18_768
DEFAULT_GEMINI_PORT = 41_243
GEMINI_READY_TIMEOUT = 60.0
CARD_PATH = "/.well-known/agent-card.json"
MAX_BODY_BYTES = 128 * 1024
MAX_TEXT_LENGTH = 16_384
MAX_TASKS = 128
MAX_ACTIVE_TASKS = 4
RETENTION_SECONDS = 600
DEFAULT_TASK_TIMEOUT = 600
DEFAULT_LEASE_BUDGET = 420
SUMMARY_LIMIT = 160


class RequestError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def object_value(value: object) -> dict:
    if not isinstance(value, dict):
        raise RequestError(-32602, "Expected a JSON object")
    return value


def text_value(value: object, name: str, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RequestError(-32602, f"Invalid {name}")
    return value


def text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def canonical_path(value: str) -> str:
    return os.path.normpath(os.path.realpath(os.path.abspath(
        os.path.expandvars(os.path.expanduser(value)))))


def same_path(left: str, right: str) -> bool:
    def normalized(value: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(value)))

    return normalized(left) == normalized(right)


def home_dir() -> Path:
    override = os.environ.get("AGENT_A2A_HOME")
    if override:
        return Path(canonical_path(override))
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(os.path.normpath(os.path.join(local, "AgentA2A")))


def app_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def read_relay_token() -> str:
    path = home_dir() / "relay-token"
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 16 or any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise ValueError("relay-token is invalid")
    return token


def read_config() -> dict:
    path = home_dir() / "config.json"
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object")
    node = config.get("node")
    if not isinstance(node, str) or not node.strip() or not os.path.isabs(node.strip()):
        raise ValueError("config.json node must be an absolute path")
    model = config.get("gemini_model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("config.json gemini_model must be non-empty text")
    return {"node": os.path.normpath(node.strip()), "gemini_model": model.strip() if isinstance(model, str) else None}


def task_message(task: "Task", role: str, message_id: str, text: str) -> dict:
    return {
        "kind": "message", "role": role, "messageId": message_id,
        "taskId": task.id, "contextId": task.context_id, "parts": [text_part(text)],
    }


@dataclass
class Task:
    message_id: str
    prompt: str
    deadline: float
    id: str = field(default_factory=lambda: str(uuid4()))
    context_id: str = field(default_factory=lambda: str(uuid4()))
    output_id: str = field(default_factory=lambda: str(uuid4()))
    state: str = "submitted"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    terminal_at: float | None = None
    lease_id: str | None = None
    claimed_at: float | None = None
    result: str | None = None
    outcome: str | None = None
    summary: str | None = None
    card_path: str | None = None
    event_id: str | None = None

    def update(self, state: str, result: str | None = None, *,
               outcome: str | None = None, summary: str | None = None,
               card_path: str | None = None, event_id: str | None = None) -> None:
        self.state = state
        self.result = result
        self.timestamp = datetime.now(timezone.utc).isoformat()
        if outcome is not None:
            self.outcome = outcome
        if summary is not None:
            self.summary = summary
        if card_path is not None:
            self.card_path = card_path
        if event_id is not None:
            self.event_id = event_id
        if state in {"completed", "canceled", "failed"}:
            self.terminal_at = monotonic()

    def snapshot(self, history_length: int = 0) -> dict:
        result = {
            "kind": "task", "id": self.id, "contextId": self.context_id,
            "status": {"state": self.state, "timestamp": self.timestamp},
        }
        if self.state == "completed":
            text = self.result if self.result is not None else (self.summary or "")
            result["artifacts"] = [{
                "artifactId": self.output_id, "name": "TRAE reply",
                "parts": [text_part(text)],
            }]
        elif self.state == "failed":
            result["status"]["message"] = task_message(
                self, "agent", self.output_id, self.result or self.summary or "")
        if self.terminal_at is not None:
            result["metadata"] = {
                "eventId": self.event_id,
                "outcome": self.outcome,
                "summary": self.summary,
                "cardPath": self.card_path,
            }
        if history_length:
            result["history"] = [task_message(self, "user", self.message_id, self.prompt)]
        return result


class RelayQueue:
    def __init__(self, task_timeout: int, wait_timeout: int, workspace: str, *,
                 lease_budget: int | None = None, results_dir: Path | None = None,
                 log_path: Path | None = None):
        self.task_timeout = task_timeout
        self.wait_timeout = wait_timeout
        self.workspace = workspace
        self.lease_budget = min(lease_budget or DEFAULT_LEASE_BUDGET, task_timeout)
        self.results_dir = results_dir
        self.log_path = log_path
        self.tasks: dict[str, Task] = {}
        self.changed = threading.Condition()
        self.stopping = False

    @staticmethod
    def _summary(text: str | None, fallback: str) -> str:
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()[:SUMMARY_LIMIT]
        return fallback[:SUMMARY_LIMIT]

    def _log(self, message: str) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
        except OSError:
            pass

    def _write_card(self, task: Task, outcome: str, text: str, summary: str) -> str | None:
        if self.results_dir is None:
            return None
        payload = {
            "schema": "trae.a2a.result_card.v2.1",
            "taskId": task.id,
            "contextId": task.context_id,
            "workspace": self.workspace,
            "status": outcome,
            "summary": summary,
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            path = self.results_dir / f"{task.id}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            self._log(f"card write failed task={task.id}: {type(error).__name__}")
            return None
        return str(path)

    def _terminal(self, task: Task, state: str, text: str, outcome: str,
                  summary: str) -> None:
        digest = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]
        event_id = f"{task.id}:{outcome}:{digest}"
        card_path = self._write_card(task, outcome, text or "", summary)
        task.update(state, text, outcome=outcome, summary=summary,
                    card_path=card_path, event_id=event_id)
        self._log(f"terminal task={task.id} state={state} outcome={outcome} event={event_id}")
        self.changed.notify_all()

    def expire(self) -> None:
        now = monotonic()
        for task in list(self.tasks.values()):
            if task.terminal_at is not None:
                if now - task.terminal_at >= RETENTION_SECONDS:
                    del self.tasks[task.id]
                continue
            if task.deadline <= now:
                self._terminal(task, "failed", "", "timeout",
                               "Task deadline reached before a result was returned")
            elif (task.state == "working" and task.claimed_at is not None
                  and task.claimed_at + self.lease_budget <= now):
                self._terminal(task, "completed", "", "partial",
                               "Lease budget exhausted before the worker returned a result")

    def counts(self) -> tuple[int, int]:
        queued = sum(task.state == "submitted" for task in self.tasks.values())
        working = sum(task.state == "working" for task in self.tasks.values())
        return queued, working

    def submit(self, params: dict) -> tuple[Task, bool]:
        message = object_value(params.get("message"))
        message_id = text_value(message.get("messageId"), "messageId", 128)
        if message.get("role") != "user" or message.get("kind", "message") != "message":
            raise RequestError(-32602, "Expected a user message")
        if message.get("taskId"):
            raise RequestError(-32004, "The desktop relay accepts standalone tasks")
        context_id = message.get("contextId")
        if context_id is not None:
            context_id = text_value(context_id, "contextId", 128)
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            raise RequestError(-32602, "Expected one or more message parts")
        if any(not isinstance(part, dict) or part.get("kind") != "text" for part in parts):
            raise RequestError(-32005, "Only text parts are supported")
        prompt = text_value("\n".join(text_value(part.get("text"), "part text") for part in parts), "prompt")
        options = object_value(params.get("configuration", {}))
        blocking = options.get("blocking", True)
        if type(blocking) is not bool:
            raise RequestError(-32602, "Expected a boolean blocking argument")
        if options.get("pushNotificationConfig") is not None:
            raise RequestError(-32003, "Push notifications are unavailable")

        with self.changed:
            if self.stopping:
                raise RequestError(-32000, "The local bridge is stopping")
            self.expire()
            task = next((item for item in self.tasks.values() if item.message_id == message_id), None)
            if task is None:
                if context_id and not any(item.context_id == context_id for item in self.tasks.values()):
                    raise RequestError(-32602, "Unknown contextId for the active workspace")
                active_count = sum(item.terminal_at is None for item in self.tasks.values())
                if len(self.tasks) >= MAX_TASKS or active_count >= MAX_ACTIVE_TASKS:
                    raise RequestError(-32000, "The desktop relay queue is full")
                task = Task(message_id, prompt, monotonic() + self.task_timeout,
                            context_id=context_id or str(uuid4()))
                self.tasks[task.id] = task
                self._log(f"queued task={task.id}")
            elif task.prompt != prompt or (context_id and task.context_id != context_id):
                raise RequestError(-32602, "messageId already belongs to a different message")

            return task, blocking

    def message(self, params: dict) -> dict:
        task, blocking = self.submit(params)
        with self.changed:
            wait_until = min(task.deadline, monotonic() + self.wait_timeout)
            while blocking and task.terminal_at is None and monotonic() < wait_until:
                self.changed.wait(max(0, wait_until - monotonic()))
                self.expire()
            return task.snapshot()

    def get_task(self, task_id: object) -> Task:
        self.expire()
        task = self.tasks.get(text_value(task_id, "task id", 128))
        if task is None:
            raise RequestError(-32001, "Task not found")
        return task

    def get(self, params: dict) -> dict:
        history_length = params.get("historyLength", 0)
        if type(history_length) is not int or history_length < 0:
            raise RequestError(-32602, "Expected a nonnegative historyLength")
        with self.changed:
            return self.get_task(params.get("id")).snapshot(history_length)

    def cancel(self, params: dict) -> dict:
        with self.changed:
            task = self.get_task(params.get("id"))
            if task.state != "submitted":
                raise RequestError(-32002, "Only tasks awaiting desktop submission can be canceled")
            self._terminal(task, "canceled", "", "failed", "Task canceled before a desktop claim")
            return task.snapshot()

    def claim(self, workspace: str | None = None) -> dict:
        with self.changed:
            if workspace is not None and not same_path(workspace, self.workspace):
                raise RequestError(-32000, f"The active workspace is {self.workspace}; run a2a use to switch")
            self.expire()
            if any(item.state == "working" for item in self.tasks.values()):
                return {}
            task = next((item for item in self.tasks.values() if item.state == "submitted"), None)
            if task is None:
                return {}
            task.lease_id = str(uuid4())
            task.claimed_at = monotonic()
            task.update("working")
            self._log(f"claimed task={task.id} lease_budget={self.lease_budget}")
            self.changed.notify_all()
            return {"taskId": task.id, "leaseId": task.lease_id, "text": task.prompt,
                    "workspace": self.workspace, "leaseBudgetSeconds": self.lease_budget}

    def finish(self, params: dict, state: str) -> dict:
        reply = text_value(params.get("text"), "reply")
        lease_id = text_value(params.get("leaseId"), "leaseId", 128)
        requested = params.get("status", "done" if state == "completed" else "failed")
        if requested not in {"done", "partial", "failed"}:
            raise RequestError(-32602, "status must be done, partial, or failed")
        if state == "failed" or requested == "failed":
            target_state, outcome = "failed", "failed"
        elif requested == "partial":
            target_state, outcome = "completed", "partial"
        else:
            target_state, outcome = "completed", "success"
        with self.changed:
            task = self.get_task(params.get("taskId"))
            if task.lease_id is None or not lease_id.isascii() or not hmac.compare_digest(task.lease_id, lease_id):
                raise RequestError(-32602, "The lease does not match this task")
            if (task.terminal_at is not None and task.state == target_state
                    and task.result == reply and task.outcome == outcome):
                return {}
            if task.state != "working":
                raise RequestError(-32004, "The desktop relay can only finish a claimed task")
            fallback = {"success": "Task completed.", "partial": "Partial result returned.",
                        "failed": "Task failed."}[outcome]
            self._terminal(task, target_state, reply, outcome, self._summary(reply, fallback))
            return {}


class GeminiRuntime:
    """启动并监督官方 Gemini CLI A2A 子进程。"""

    def __init__(self, node: str, script: Path, workspace: str, port: int,
                 model: str | None, log_path: Path, on_exit):
        self.node = node
        self.script = script
        self.workspace = workspace
        self.port = port
        self.model = model
        self.log_path = log_path
        self.on_exit = on_exit
        self.process: subprocess.Popen | None = None
        self.state = "starting"
        self.exit_code: int | None = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        if not Path(self.node).is_file():
            raise FileNotFoundError(f"Node.js not found: {self.node}")
        if not self.script.is_file():
            raise FileNotFoundError(f"Node launcher not found: {self.script}")
        environment = os.environ.copy()
        environment.pop("AGENT_A2A_SESSION_TOKEN", None)
        environment["CODER_AGENT_WORKSPACE_PATH"] = self.workspace
        environment["CODER_AGENT_ALLOWED_ROOT"] = self.workspace
        environment["CODER_AGENT_PORT"] = str(self.port)
        environment["GEMINI_CLI_TRUST_WORKSPACE"] = "false"
        environment["GEMINI_YOLO_MODE"] = "false"
        if self.model:
            environment["AGENT_A2A_GEMINI_MODEL"] = self.model
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [self.node, str(self.script)], cwd=self.workspace, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
        threading.Thread(target=self._monitor, daemon=True).start()

    def _monitor(self) -> None:
        process = self.process
        assert process is not None
        deadline = monotonic() + GEMINI_READY_TIMEOUT
        ready = False
        while not self._stopped.is_set() and monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection((LOOPBACK, self.port), timeout=0.3):
                    ready = True
                    break
            except OSError:
                time.sleep(0.1)
        if ready:
            with self._lock:
                self.state = "running"
        if not ready and process.poll() is None and not self._stopped.is_set():
            self._write_log("Gemini did not listen before the readiness deadline; stopping it")
            try:
                process.terminate()
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
            except OSError:
                pass
        code = process.wait()
        with self._lock:
            self.state = "exited"
            self.exit_code = code
        self._write_log(f"Gemini child exited with code {code}")
        if not self._stopped.is_set():
            self.on_exit()

    def stop(self) -> None:
        self._stopped.set()
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        except OSError:
            pass

    def status(self) -> dict:
        with self._lock:
            return {"state": self.state, "pid": self.process.pid if self.process else None,
                    "exitCode": self.exit_code}

    def _write_log(self, message: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
        except OSError:
            pass


def agent_card(url: str) -> dict:
    return {
        "protocolVersion": "0.3.0",
        "name": "TRAE workspace relay",
        "description": "Delegates standalone text tasks to the TRAE desktop session bound to one local workspace.",
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": "2.1.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "trae-workspace-task", "name": "Run a TRAE workspace task",
            "description": "Ask TRAE desktop to carry out an isolated text task in the active workspace.",
            "tags": ["desktop", "coding", "workspace"],
        }],
    }


class Gateway(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, port: int, queue: RelayQueue, relay_token: str, session_token: str,
                 workspace: str, gemini_port: int, gemini: GeminiRuntime | None = None):
        super().__init__((LOOPBACK, port), Handler)
        self.queue = queue
        self.workspace = workspace
        self.gemini_port = gemini_port
        self.gemini = gemini
        self.session_token = session_token
        self.relay_auth = ("Bearer " + relay_token).encode()
        self.session_auth = ("Bearer " + session_token).encode()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.card = agent_card(f"http://{LOOPBACK}:{self.server_port}/")


class Handler(BaseHTTPRequestHandler):
    server: Gateway
    timeout = 10

    def log_message(self, format: str, *args) -> None:
        # 不记录用户文本、URL 或 Authorization。
        pass

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self, expected: bytes) -> bool:
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected)

    def do_GET(self) -> None:
        if self.path == CARD_PATH:
            self.reply(200, self.server.card)
        elif self.path == "/_admin/health":
            self.handle_health()
        else:
            self.reply(404, {"error": "Not found"})

    def read_json(self) -> object:
        if self.headers.get_content_type() != "application/json" or self.headers.get("Origin"):
            raise RequestError(-32600, "Use application/json from a local client")
        length = self.headers.get("Content-Length", "")
        if len(length) > 9 or not length.isascii() or not length.isdigit() or not 0 < int(length) <= MAX_BODY_BYTES:
            raise RequestError(-32600, "Missing or invalid Content-Length")
        try:
            return json.loads(self.rfile.read(int(length)))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, TimeoutError) as exc:
            raise RequestError(-32700, "Invalid JSON") from exc

    def do_POST(self) -> None:
        if self.path == "/":
            self.handle_rpc()
        elif self.path in {"/_relay/claim", "/_relay/complete", "/_relay/fail"}:
            self.handle_relay()
        elif self.path == "/_admin/stop":
            self.handle_stop()
        else:
            self.reply(404, {"error": "Not found"})

    def handle_rpc(self) -> None:
        if not self.authorized(self.server.session_auth):
            self.reply(401, {"error": "Missing or invalid session credential"})
            return
        request_id = None
        try:
            request = self.read_json()
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise RequestError(-32600, "Invalid JSON-RPC request")
            request_id = request.get("id")
            method = request.get("method")
            if type(request_id) not in {str, int} or not isinstance(method, str):
                request_id = None
                raise RequestError(-32600, "A request id and method are required")
            params = object_value(request.get("params", {}))
            if method == "message/stream":
                task, _ = self.server.queue.submit(params)
                self.stream_task(request_id, task)
                return
            if method == "tasks/resubscribe":
                with self.server.queue.changed:
                    task = self.server.queue.get_task(params.get("id"))
                self.stream_task(request_id, task)
                return
            operations = {
                "message/send": self.server.queue.message,
                "tasks/get": self.server.queue.get,
                "tasks/cancel": self.server.queue.cancel,
            }
            if method not in operations:
                raise RequestError(-32601, "Method not found")
            result = operations[method](params)
            self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except RequestError as exc:
            self.reply(200, {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": exc.code, "message": str(exc)},
            })

    def stream_task(self, request_id: str | int, task: Task) -> None:
        queue = self.server.queue

        def event(result: dict) -> None:
            frame = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
            self.wfile.write(f"data: {frame}\n\n".encode())
            self.wfile.flush()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            with queue.changed:
                snapshot = task.snapshot()
            identity = {"taskId": task.id, "contextId": task.context_id}
            initial = {key: snapshot[key] for key in ("kind", "id", "contextId", "status")}
            if snapshot.get("metadata"):
                initial["metadata"] = snapshot["metadata"]
            event(initial)
            last_state = snapshot["status"]["state"]
            while True:
                with queue.changed:
                    queue.expire()
                    if task.terminal_at is None and task.state == last_state:
                        queue.changed.wait(min(5, max(0, task.deadline - monotonic())))
                        queue.expire()
                    snapshot = task.snapshot()
                    final = task.terminal_at is not None
                status = snapshot["status"]
                for artifact in snapshot.get("artifacts", []):
                    event({"kind": "artifact-update", **identity, "artifact": artifact,
                           "append": False, "lastChunk": True})
                if final or status["state"] != last_state:
                    update = {"kind": "status-update", **identity, "status": status, "final": final}
                    if snapshot.get("metadata"):
                        update["metadata"] = snapshot["metadata"]
                    event(update)
                    if final:
                        return
                    last_state = status["state"]
                else:
                    self.wfile.write(b": waiting\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def handle_relay(self) -> None:
        if not self.authorized(self.server.relay_auth):
            self.reply(401, {"error": "Missing or invalid relay credential"})
            return
        try:
            params = object_value(self.read_json())
            if self.path == "/_relay/claim":
                workspace = params.get("workspace")
                if workspace is not None and not isinstance(workspace, str):
                    raise RequestError(-32602, "Invalid workspace")
                result = self.server.queue.claim(workspace)
            else:
                state = "completed" if self.path == "/_relay/complete" else "failed"
                result = self.server.queue.finish(params, state)
            self.reply(200, result)
        except RequestError as exc:
            self.reply(400, {"error": str(exc)})

    def handle_health(self) -> None:
        if not self.authorized(self.server.relay_auth):
            self.reply(401, {"error": "Missing or invalid relay credential"})
            return
        with self.server.queue.changed:
            self.server.queue.expire()
            queued, working = self.server.queue.counts()
        self.reply(200, {
            "status": "ok",
            "pid": os.getpid(),
            "workspace": self.server.workspace,
            "port": self.server.server_port,
            "geminiPort": self.server.gemini_port,
            "protocolVersion": "0.3.0",
            "sessionToken": self.server.session_token,
            "startedAt": self.server.started_at,
            "tasks": {"queued": queued, "working": working, "total": len(self.server.queue.tasks)},
            "taskTimeout": self.server.queue.task_timeout,
            "leaseBudget": self.server.queue.lease_budget,
            "gemini": self.server.gemini.status() if self.server.gemini else {"state": "disabled"},
        })

    def handle_stop(self) -> None:
        if not self.authorized(self.server.relay_auth):
            self.reply(401, {"error": "Missing or invalid relay credential"})
            return
        with self.server.queue.changed:
            self.server.queue.expire()
            queued, working = self.server.queue.counts()
            busy = bool(queued or working or self.server.queue.stopping)
            if not busy:
                self.server.queue.stopping = True
        if busy:
            self.reply(409, {
                "error": "The active workspace still has queued or running TRAE tasks",
                "queued": queued, "working": working,
            })
            return
        self.reply(200, {"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Absolute active workspace")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--gemini-port", type=int, default=DEFAULT_GEMINI_PORT)
    parser.add_argument("--task-timeout", type=int, default=DEFAULT_TASK_TIMEOUT)
    parser.add_argument("--lease-budget", type=int, default=DEFAULT_LEASE_BUDGET)
    parser.add_argument("--wait-timeout", type=int, default=90)
    args = parser.parse_args()

    workspace = canonical_path(args.workspace)
    if not os.path.isabs(workspace) or not os.path.isdir(workspace):
        parser.error("--workspace must be an existing absolute directory")
    if not 0 <= args.port <= 65_535 or not 0 < args.gemini_port <= 65_535:
        parser.error("Choose valid ports")
    args.wait_timeout = max(1, min(args.wait_timeout, args.task_timeout))
    if not 0 < args.lease_budget <= args.task_timeout:
        parser.error("Choose a lease budget no longer than the task timeout")

    try:
        relay_token = read_relay_token()
        config = read_config()
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr, flush=True)
        return 2
    session_token = os.environ.get("AGENT_A2A_SESSION_TOKEN", "")
    if len(session_token) < 16 or any(ord(character) < 33 or ord(character) > 126 for character in session_token):
        print("A random AGENT_A2A_SESSION_TOKEN is required", file=sys.stderr, flush=True)
        return 2

    log_path = home_dir() / "gateway.log"
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} starting workspace={workspace} "
                         f"receiver={args.port} gemini={args.gemini_port}\n")
    except OSError:
        pass

    queue = RelayQueue(
        args.task_timeout, args.wait_timeout, workspace,
        lease_budget=args.lease_budget, results_dir=home_dir() / "results", log_path=log_path,
    )
    try:
        server = Gateway(args.port, queue, relay_token, session_token, workspace, args.gemini_port)
    except OSError as error:
        print(f"Unable to bind 127.0.0.1:{args.port}: {error}", file=sys.stderr, flush=True)
        return 2

    runtime = GeminiRuntime(
        config["node"], app_dir() / "node" / "run-gemini-a2a.mjs",
        workspace, args.gemini_port, config["gemini_model"], log_path, server.shutdown,
    )
    server.gemini = runtime
    try:
        runtime.start()
    except OSError as error:
        print(f"Unable to start the Gemini child: {error}", file=sys.stderr, flush=True)
        server.server_close()
        return 2
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
