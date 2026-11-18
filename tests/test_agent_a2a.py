"""跨工作区 A2A 核心的本机自测（不访问网络、不使用真实凭证）。"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import agent_a2a as core  # noqa: E402
import trae_a2a_gateway as gateway_mod  # noqa: E402


RELAY_TOKEN = "relay-token-for-tests-0123456789"
SESSION_TOKEN = "session-token-for-tests-0123456789"

FAKE_NODE = r"""
import { createServer } from 'node:http';
import { writeFileSync } from 'node:fs';

const port = Number(process.env.CODER_AGENT_PORT);
const probe = process.env.AGENT_A2A_TEST_PROBE;
if (probe) {
  writeFileSync(probe, JSON.stringify({
    cwd: process.cwd(),
    workspace: process.env.CODER_AGENT_WORKSPACE_PATH,
    allowed: process.env.CODER_AGENT_ALLOWED_ROOT,
    port: process.env.CODER_AGENT_PORT,
    trust: process.env.GEMINI_CLI_TRUST_WORKSPACE,
    yolo: process.env.GEMINI_YOLO_MODE,
    model: process.env.AGENT_A2A_GEMINI_MODEL ?? null,
    session: process.env.AGENT_A2A_SESSION_TOKEN ?? null,
  }) + '\n', { flag: 'a' });
}
if (process.env.AGENT_A2A_SESSION_TOKEN) process.exit(9);

const parent = process.ppid;
setInterval(() => {
  try { process.kill(parent, 0); } catch { process.exit(0); }
}, 500);

const card = {
  protocolVersion: '0.3.0',
  name: 'Fake Gemini A2A',
  description: 'Loopback test double',
  url: `http://127.0.0.1:${port}/`,
  preferredTransport: 'JSONRPC',
  version: '1.0.0',
  capabilities: { streaming: false, pushNotifications: false },
  defaultInputModes: ['text/plain'],
  defaultOutputModes: ['text/plain'],
  skills: [],
};

const server = createServer(async (request, response) => {
  if (request.method === 'GET' && request.url === '/.well-known/agent-card.json') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify(card));
    return;
  }
  if (request.method === 'POST' && request.url === '/') {
    let body = '';
    for await (const chunk of request) body += chunk;
    const message = JSON.parse(body || '{}');
    const text = (message.params?.message?.parts ?? []).map((part) => part.text ?? '').join('\n');
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({
      jsonrpc: '2.0',
      id: message.id,
      result: {
        kind: 'task',
        id: `fake-${Math.random().toString(16).slice(2)}`,
        contextId: 'fake-context',
        status: { state: 'completed', timestamp: new Date().toISOString() },
        artifacts: [{ artifactId: 'fake-output', name: 'echo', parts: [{ kind: 'text', text: `echo:${text}` }] }],
      },
    }));
    return;
  }
  response.statusCode = 404;
  response.end('{}');
});
server.listen(port, '127.0.0.1');
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http_json(port: int, path: str, *, method: str = "GET", body: object = None,
              bearer: str | None = None, timeout: float = 5.0) -> tuple[int, object]:
    headers = {}
    data = None
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before the deadline")


class GatewayProtocolTests(unittest.TestCase):
    wait_timeout = 3

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="a2a-gw-")
        self.other = tempfile.mkdtemp(prefix="a2a-other-")
        self.server = gateway_mod.Gateway(
            0, gateway_mod.RelayQueue(600, self.wait_timeout, self.workspace),
            RELAY_TOKEN, SESSION_TOKEN, self.workspace, 0,
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.addCleanup(shutil.rmtree, self.other, True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def rpc(self, method: str, params: dict, *, bearer: str = SESSION_TOKEN, request_id: str = "1"):
        return http_json(self.port, "/", method="POST", bearer=bearer, body={
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        })

    def stream_rpc(self, method: str, params: dict, *, request_id: str):
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        headers = {"Authorization": f"Bearer {SESSION_TOKEN}",
                   "Content-Type": "application/json", "Accept": "text/event-stream"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/", body, headers)
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "text/event-stream")

        data = []
        for line in response:
            if line.startswith(b"data:"):
                data.append(line.removeprefix(b"data:").strip())
            elif not line.strip() and data:
                event = json.loads(b"\n".join(data))
                self.assertEqual(event["jsonrpc"], "2.0")
                self.assertEqual(event["id"], request_id)
                yield event["result"]
                data.clear()

    def test_card_session_auth_queue_and_lease(self):
        status, card = http_json(self.port, "/.well-known/agent-card.json")
        self.assertEqual(status, 200)
        self.assertEqual(card["protocolVersion"], "0.3.0")

        self.assertEqual(self.rpc("message/send", {}, bearer="wrong")[0], 401)
        message = {
            "kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "text", "text": "hello"}],
        }
        status, payload = self.rpc("message/send", {
            "message": message, "configuration": {"blocking": False},
        })
        self.assertEqual(status, 200)
        task_id = payload["result"]["id"]
        self.assertEqual(payload["result"]["status"]["state"], "submitted")

        status, health = http_json(self.port, "/_admin/health", bearer=RELAY_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(health["workspace"], self.workspace)
        self.assertEqual(health["sessionToken"], SESSION_TOKEN)
        self.assertEqual(health["tasks"]["queued"], 1)

        status, _ = http_json(self.port, "/_admin/stop", method="POST", body={}, bearer=RELAY_TOKEN)
        self.assertEqual(status, 409)

        status, payload = http_json(self.port, "/_relay/claim", method="POST",
                                    body={"workspace": self.other}, bearer=RELAY_TOKEN)
        self.assertEqual(status, 400)
        status, payload = http_json(self.port, "/_relay/claim", method="POST",
                                    body={"workspace": self.workspace}, bearer=RELAY_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["taskId"], task_id)

        status, _ = http_json(self.port, "/_relay/complete", method="POST", bearer=RELAY_TOKEN, body={
            "taskId": task_id, "leaseId": "not-the-lease", "text": "reply",
        })
        self.assertEqual(status, 400)
        status, _ = http_json(self.port, "/_relay/complete", method="POST", bearer=RELAY_TOKEN, body={
            "taskId": task_id, "leaseId": payload["leaseId"], "text": "reply",
        })
        self.assertEqual(status, 200)

        status, payload = self.rpc("tasks/get", {"id": task_id, "historyLength": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"]["state"], "completed")
        self.assertEqual(payload["result"]["artifacts"][0]["parts"][0]["text"], "reply")

        status, _ = http_json(self.port, "/_admin/stop", method="POST", body={}, bearer=RELAY_TOKEN)
        self.assertEqual(status, 200)
        wait_for(lambda: not self.thread.is_alive(), timeout=5)

    def test_message_stream_delivers_reply_after_blocking_send_timeout(self):
        status, card = http_json(self.port, "/.well-known/agent-card.json")
        self.assertEqual(status, 200)
        self.assertIs(card["capabilities"]["streaming"], True)

        started = time.monotonic()
        events = self.stream_rpc("message/stream", {"message": {
            "kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "text", "text": "hello"}],
        }}, request_id="long-desktop-task")
        task = next(events)
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["status"]["state"], "submitted")

        status, claim = http_json(self.port, "/_relay/claim", method="POST", bearer=RELAY_TOKEN,
                                  body={"workspace": self.workspace})
        self.assertEqual(status, 200)
        self.assertEqual(claim["taskId"], task["id"])
        time.sleep(max(0.0, started + self.wait_timeout + 0.25 - time.monotonic()))
        status, _ = http_json(self.port, "/_relay/complete", method="POST", bearer=RELAY_TOKEN, body={
            "taskId": task["id"], "leaseId": claim["leaseId"], "text": "exact delayed reply",
        })
        self.assertEqual(status, 200)

        updates = list(events)
        artifacts = [event for event in updates if event["kind"] == "artifact-update"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["taskId"], task["id"])
        self.assertEqual(artifacts[0]["contextId"], task["contextId"])
        self.assertEqual(artifacts[0]["artifact"]["parts"][0]["text"], "exact delayed reply")
        self.assertEqual(updates[-1]["kind"], "status-update")
        self.assertEqual(updates[-1]["taskId"], task["id"])
        self.assertEqual(updates[-1]["status"]["state"], "completed")
        self.assertIs(updates[-1]["final"], True)

    def test_tasks_resubscribe_receives_result_for_existing_task(self):
        status, payload = self.rpc("message/send", {
            "message": {
                "kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                "parts": [{"kind": "text", "text": "hello"}],
            },
            "configuration": {"blocking": False},
        })
        self.assertEqual(status, 200)
        task = payload["result"]
        events = self.stream_rpc("tasks/resubscribe", {"id": task["id"]}, request_id="reconnect")
        self.assertEqual(next(events), task)

        status, claim = http_json(self.port, "/_relay/claim", method="POST", bearer=RELAY_TOKEN,
                                  body={"workspace": self.workspace})
        self.assertEqual(status, 200)
        self.assertEqual(claim["taskId"], task["id"])
        status, _ = http_json(self.port, "/_relay/complete", method="POST", bearer=RELAY_TOKEN, body={
            "taskId": task["id"], "leaseId": claim["leaseId"], "text": "reply after reconnect",
        })
        self.assertEqual(status, 200)
        updates = list(events)
        self.assertEqual(updates[-2]["artifact"]["parts"][0]["text"], "reply after reconnect")
        self.assertIs(updates[-1]["final"], True)

    def test_new_message_reuses_known_context_without_reusing_task(self):
        def submit(text, **reference):
            status, payload = self.rpc("message/send", {
                "message": {
                    "kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                    "parts": [{"kind": "text", "text": text}], **reference,
                },
                "configuration": {"blocking": False},
            })
            self.assertEqual(status, 200)
            self.assertIn("result", payload, payload)
            return payload["result"]

        original = submit("first task")
        continuation = submit("second task", contextId=original["contextId"])
        self.assertNotEqual(continuation["id"], original["id"])
        self.assertEqual(continuation["contextId"], original["contextId"])
        self.assertEqual(continuation["status"]["state"], "submitted")


class CliLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="a2a-cli-")
        self.home = Path(self.root) / "home"
        self.home.mkdir()
        self.app = Path(self.root) / "app"
        (self.app / "tools").mkdir(parents=True)
        (self.app / "node").mkdir()
        for name in ("agent_a2a.py", "trae_a2a_gateway.py", "trae_gemini_mcp.py"):
            shutil.copy2(TOOLS / name, self.app / "tools" / name)
        (self.app / "node" / "run-gemini-a2a.mjs").write_text(FAKE_NODE, encoding="utf-8")
        (self.app / "node" / "gemini-loopback.mjs").write_text("// test shim\n", encoding="utf-8")
        bundle = self.app / "node_modules" / "@google" / "gemini-cli" / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "gemini.js").write_text(
            "console.log(JSON.stringify({args: process.argv.slice(2), "
            "origin: process.env.AGENT_A2A_TRAE_ORIGIN, "
            "token: Boolean(process.env.AGENT_A2A_SESSION_TOKEN)}));\n",
            encoding="utf-8",
        )
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("Node.js is required for lifecycle tests")
        (self.home / "relay-token").write_text(RELAY_TOKEN, encoding="utf-8")
        (self.home / "config.json").write_text(json.dumps({
            "node": os.path.abspath(self.node), "gemini_model": "test-model",
        }), encoding="utf-8")
        raw_a = tempfile.mkdtemp(prefix="a2a-ws-a-")
        self.workspace_a = core.canonical_workspace(raw_a)
        self.short_a = raw_a
        self.workspace_b = core.canonical_workspace(tempfile.mkdtemp(prefix="a2a-ws-b-"))
        self.probe = Path(self.root) / "probe.jsonl"
        self.receiver = free_port()
        self.gemini = free_port()
        while self.gemini == self.receiver:
            self.gemini = free_port()
        self.env = os.environ.copy()
        for key in ("AGENT_A2A_SESSION_TOKEN", "AGENT_A2A_TRAE_WORKSPACE", "GEMINI_A2A_URL",
                    "TRAE_A2A_GATEWAY_URL", "TRAE_A2A_RELAY_TOKEN"):
            self.env.pop(key, None)
        self.env.update({
            "AGENT_A2A_HOME": str(self.home),
            "AGENT_A2A_RECEIVER_PORT": str(self.receiver),
            "AGENT_A2A_GEMINI_PORT": str(self.gemini),
            "AGENT_A2A_TEST_PROBE": str(self.probe),
            "PYTHONIOENCODING": "utf-8",
        })
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.workspace_a, True)
        self.addCleanup(shutil.rmtree, self.workspace_b, True)
        self.addCleanup(self._force_cleanup)

    def run_cli(self, *arguments: str, cwd: str | None = None, timeout: float = 90):
        return subprocess.run(
            [sys.executable, "-B", str(self.app / "tools" / "agent_a2a.py"), *arguments],
            cwd=cwd or self.workspace_a, env=self.env, capture_output=True,
            text=True, encoding="utf-8", timeout=timeout,
        )

    def health(self):
        try:
            status, payload = http_json(self.receiver, "/_admin/health",
                                        bearer=RELAY_TOKEN, timeout=1)
        except OSError:
            return None
        return payload if status == 200 else None

    def _force_cleanup(self):
        self.run_cli("stop", timeout=30)
        health = self.health()
        if health:
            subprocess.run(["taskkill", "/PID", str(health["pid"]), "/T", "/F"],
                           capture_output=True)

    def use(self, workspace: str):
        result = self.run_cli("use", "--workspace", workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def queue_task(self, text: str) -> tuple[str, str]:
        health = wait_for(self.health)
        status, payload = http_json(self.receiver, "/", method="POST",
                                    bearer=health["sessionToken"], body={
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {
                "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                            "parts": [{"kind": "text", "text": text}]},
                "configuration": {"blocking": False},
            },
        })
        self.assertEqual(status, 200)
        return payload["result"]["id"], health["sessionToken"]

    def claim(self, workspace: str):
        status, payload = http_json(self.receiver, "/_relay/claim", method="POST",
                                    bearer=RELAY_TOKEN, body={"workspace": workspace})
        self.assertEqual(status, 200)
        return payload

    def complete(self, task: dict, text: str):
        status, payload = http_json(self.receiver, "/_relay/complete", method="POST",
                                    bearer=RELAY_TOKEN, body={
            "taskId": task["taskId"], "leaseId": task["leaseId"], "text": text,
        })
        self.assertEqual(status, 200)
        return payload

    def test_activation_environment_and_gemini_route(self):
        output = self.use(self.short_a)
        self.assertTrue(output["active"])
        self.assertTrue(output["started"])
        self.assertTrue(core.same_workspace(output["workspace"], self.workspace_a))
        self.assertEqual(output["workspace"], self.workspace_a)
        self.assertNotIn("sessionToken", json.dumps(output))

        probe = json.loads(self.probe.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(os.path.normcase(probe["cwd"]), os.path.normcase(self.workspace_a))
        self.assertEqual(os.path.normcase(probe["workspace"]), os.path.normcase(self.workspace_a))
        self.assertEqual(os.path.normcase(probe["allowed"]), os.path.normcase(self.workspace_a))
        self.assertEqual(probe["port"], str(self.gemini))
        self.assertEqual(probe["trust"], "false")
        self.assertEqual(probe["yolo"], "false")
        self.assertEqual(probe["model"], "test-model")
        self.assertIsNone(probe["session"])

        result = self.run_cli("send", "--to", "gemini", "--text", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["text"], "echo:hello")
        self.assertEqual(payload["state"], "completed")

        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["workspace"], self.workspace_a)
        self.assertNotIn("sessionToken", result.stdout)

    def test_concurrent_sends_are_serialized_by_lease(self):
        self.use(self.workspace_a)
        tasks = ["first", "second"]
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", str(self.app / "tools" / "agent_a2a.py"),
                 "send", "--to", "trae", "--text", text, "--timeout", "30"],
                cwd=self.workspace_a, env=self.env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for text in tasks
        ]
        wait_for(lambda: (self.health() or {}).get("tasks", {}).get("queued") == 2)
        replies = {}
        for _ in tasks:
            task = self.claim(self.workspace_a)
            self.assertTrue(task)
            reply = f"handled:{task['text']}"
            self.complete(task, reply)
            replies[task["text"]] = reply
        for process, text in zip(processes, tasks):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["text"], replies[text])
            self.assertEqual(payload["state"], "completed")
        wait_for(lambda: (self.health() or {}).get("tasks", {}).get("queued") == 0)

    def test_switch_refuses_active_tasks_and_rotates_session(self):
        self.use(self.workspace_a)
        old_session = self.health()["sessionToken"]
        task_id, _ = self.queue_task("still pending")

        result = self.run_cli("use", "--workspace", self.workspace_b)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝切换", result.stderr)
        self.assertEqual(self.health()["workspace"], self.workspace_a)

        status, _ = http_json(self.receiver, "/", method="POST", bearer=old_session, body={
            "jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel",
            "params": {"id": task_id},
        })
        self.assertEqual(status, 200)

        output = self.use(self.workspace_b)
        self.assertEqual(output["workspace"], self.workspace_b)
        new_session = self.health()["sessionToken"]
        self.assertNotEqual(old_session, new_session)

        status, _ = http_json(self.receiver, "/", method="POST", bearer=old_session, body={
            "jsonrpc": "2.0", "id": "stale", "method": "message/send",
            "params": {
                "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                            "parts": [{"kind": "text", "text": "stale"}]},
                "configuration": {"blocking": False},
            },
        })
        self.assertEqual(status, 401)

        status, payload = http_json(self.receiver, "/_relay/claim", method="POST",
                                    bearer=RELAY_TOKEN, body={"workspace": self.workspace_a})
        self.assertEqual(status, 400)

        task_id, _ = self.queue_task("pending in b")
        result = self.run_cli("stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝停止", result.stderr)
        status, _ = http_json(self.receiver, "/", method="POST", bearer=new_session, body={
            "jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel",
            "params": {"id": task_id},
        })
        self.assertEqual(status, 200)
        result = self.run_cli("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["stopped"])
        self.assertIsNone(self.health())

    def test_unknown_port_occupant_is_reported_not_killed(self):
        self.use(self.workspace_a)
        result = self.run_cli("stop")
        self.assertEqual(result.returncode, 0, result.stderr)

        occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupant.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupant.bind(("127.0.0.1", self.receiver))
        occupant.listen(4)
        try:
            result = self.run_cli("use", "--workspace", self.workspace_b)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("占用", result.stderr)
            with socket.create_connection(("127.0.0.1", self.receiver), timeout=1):
                pass
        finally:
            occupant.close()

    def test_send_from_another_cwd_is_rejected(self):
        self.use(self.workspace_a)
        for target in ("gemini", "trae"):
            result = self.run_cli("send", "--to", target, "--text", "wrong project",
                                  cwd=self.workspace_b)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("当前目录", result.stderr)
        self.assertEqual(self.health()["workspace"], self.workspace_a)

    def test_junction_resolves_to_the_same_workspace(self):
        self.use(self.workspace_a)
        junction = Path(self.root) / "link-a"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), self.workspace_a],
            capture_output=True, text=True,
        )
        if created.returncode != 0:
            self.skipTest("Unable to create a directory junction")
        self.addCleanup(subprocess.run, ["cmd", "/c", "rmdir", str(junction)],
                        capture_output=True)
        output = self.use(str(junction))
        self.assertFalse(output["started"])
        self.assertTrue(core.same_workspace(output["workspace"], self.workspace_a))

    def test_gateway_log_excludes_task_text_and_token(self):
        self.use(self.workspace_a)
        marker = "PRIVATE-TASK-TEXT-9f7c2e"
        result = self.run_cli("send", "--to", "gemini", "--text", marker)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.home / "gateway.log").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(marker, log)
        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn("sessionToken", status.stdout)
        self.assertNotIn(self.health()["sessionToken"], status.stdout)

    def test_gemini_command_uses_native_bundle_path(self):
        with unittest.mock.patch.dict(os.environ, {"AGENT_A2A_HOME": str(self.home)}):
            command = core.gemini_command(["--version"])
        self.assertEqual(command[1], "--import")
        self.assertTrue(command[2].startswith("file:///"))
        self.assertIn("gemini-loopback.mjs", command[2])
        self.assertFalse(command[3].startswith("file://"))
        self.assertTrue(command[3].endswith("gemini.js"))
        self.assertEqual(command[4], "--version")

    def test_gemini_launch_passes_arguments_and_session(self):
        self.use(self.workspace_a)
        result = self.run_cli("gemini", "--version", "--extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["args"], ["--version", "--extra"])
        self.assertEqual(payload["origin"], f"http://127.0.0.1:{self.receiver}")
        self.assertTrue(payload["token"])


class ManagedMcpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="a2a-mcp-")
        self.home = Path(self.root) / "home"
        self.home.mkdir()
        self.app = Path(self.root) / "app"
        (self.app / "tools").mkdir(parents=True)
        (self.app / "node").mkdir()
        for name in ("agent_a2a.py", "trae_a2a_gateway.py", "trae_gemini_mcp.py"):
            shutil.copy2(TOOLS / name, self.app / "tools" / name)
        (self.app / "node" / "run-gemini-a2a.mjs").write_text(FAKE_NODE, encoding="utf-8")
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("Node.js is required for managed MCP tests")
        (self.home / "relay-token").write_text(RELAY_TOKEN, encoding="utf-8")
        (self.home / "config.json").write_text(json.dumps({
            "node": os.path.abspath(self.node),
        }), encoding="utf-8")
        self.workspace_a = core.canonical_workspace(tempfile.mkdtemp(prefix="a2a-mcp-a-"))
        self.workspace_b = core.canonical_workspace(tempfile.mkdtemp(prefix="a2a-mcp-b-"))
        self.receiver = free_port()
        self.gemini = free_port()
        while self.gemini == self.receiver:
            self.gemini = free_port()
        self.env = os.environ.copy()
        for key in ("AGENT_A2A_SESSION_TOKEN", "AGENT_A2A_TRAE_WORKSPACE", "GEMINI_A2A_URL",
                    "TRAE_A2A_GATEWAY_URL", "TRAE_A2A_RELAY_TOKEN"):
            self.env.pop(key, None)
        self.env.update({
            "AGENT_A2A_HOME": str(self.home),
            "AGENT_A2A_RECEIVER_PORT": str(self.receiver),
            "AGENT_A2A_GEMINI_PORT": str(self.gemini),
            "PYTHONIOENCODING": "utf-8",
        })
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.workspace_a, True)
        self.addCleanup(shutil.rmtree, self.workspace_b, True)
        self.addCleanup(self._force_cleanup)

    def run_cli(self, *arguments: str, cwd: str | None = None, timeout: float = 90):
        return subprocess.run(
            [sys.executable, "-B", str(self.app / "tools" / "agent_a2a.py"), *arguments],
            cwd=cwd or self.workspace_a, env=self.env, capture_output=True,
            text=True, encoding="utf-8", timeout=timeout,
        )

    def health(self):
        try:
            status, payload = http_json(self.receiver, "/_admin/health",
                                        bearer=RELAY_TOKEN, timeout=1)
        except OSError:
            return None
        return payload if status == 200 else None

    def _force_cleanup(self):
        self.run_cli("stop", timeout=30)
        health = self.health()
        if health:
            subprocess.run(["taskkill", "/PID", str(health["pid"]), "/T", "/F"],
                           capture_output=True)

    def use(self, workspace: str):
        result = self.run_cli("use", "--workspace", workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_mcp(self, calls: list[dict], *, expected_workspace: str) -> list[dict]:
        environment = dict(self.env)
        environment["AGENT_A2A_TRAE_WORKSPACE"] = expected_workspace
        lines = "".join(json.dumps(call) + "\n" for call in calls)
        result = subprocess.run(
            [sys.executable, "-B", str(self.app / "tools" / "trae_gemini_mcp.py"), "--managed"],
            cwd=self.workspace_a, env=environment, input=lines, capture_output=True,
            text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def queue_task(self, text: str) -> str:
        health = wait_for(self.health)
        status, payload = http_json(self.receiver, "/", method="POST",
                                    bearer=health["sessionToken"], body={
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {
                "message": {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
                            "parts": [{"kind": "text", "text": text}]},
                "configuration": {"blocking": False},
            },
        })
        self.assertEqual(status, 200)
        return payload["result"]["id"]

    @staticmethod
    def tool_result(response: dict) -> tuple[dict | str, bool]:
        result = response["result"]
        text = result["content"][0]["text"]
        try:
            payload: dict | str = json.loads(text)
        except ValueError:
            payload = text
        return payload, bool(result.get("isError"))

    def test_managed_tools_validate_workspace_and_lease(self):
        self.use(self.workspace_a)
        queued_id = self.queue_task("mcp task")
        responses = self.run_mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "claim_trae_task", "arguments": {"workspace": self.workspace_b}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "claim_trae_task", "arguments": {"workspace": self.workspace_a}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "complete_trae_task", "arguments": {
                    "task_id": queued_id, "lease_id": "wrong-lease", "text": "bad"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
                "name": "gemini_a2a_send", "arguments": {
                    "workspace": self.workspace_a, "text": "mcp hello"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                "name": "gemini_a2a_send", "arguments": {
                    "workspace": self.workspace_b, "text": "wrong workspace"}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
                "name": "gemini_a2a_send", "arguments": {
                    "workspace": "relative-project", "text": "relative"}}},
        ], expected_workspace=self.workspace_a)
        self.assertEqual(len(responses), 8)

        tools = responses[1]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools},
                         {"gemini_a2a_send", "claim_trae_task", "complete_trae_task"})
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.assertIn("workspace", schemas["gemini_a2a_send"]["required"])
        self.assertIn("workspace", schemas["claim_trae_task"]["required"])

        payload, failed = self.tool_result(responses[2])
        self.assertTrue(failed)
        self.assertIn("不一致", payload)

        claimed, failed = self.tool_result(responses[3])
        self.assertFalse(failed)
        self.assertEqual(claimed["taskId"], queued_id)

        payload, failed = self.tool_result(responses[4])
        self.assertTrue(failed)
        self.assertIn("lease", payload)

        replies = self.run_mcp([
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
                "name": "complete_trae_task", "arguments": {
                    "task_id": queued_id, "lease_id": claimed["leaseId"], "text": "mcp done"}}},
        ], expected_workspace=self.workspace_a)
        payload, failed = self.tool_result(replies[0])
        self.assertFalse(failed)

        health = self.health()
        status, task = http_json(self.receiver, "/", method="POST",
                                 bearer=health["sessionToken"], body={
            "jsonrpc": "2.0", "id": "get", "method": "tasks/get",
            "params": {"id": queued_id, "historyLength": 1},
        })
        self.assertEqual(status, 200)
        self.assertEqual(task["result"]["status"]["state"], "completed")

        sent, failed = self.tool_result(responses[5])
        self.assertFalse(failed)
        self.assertEqual(sent["text"], "echo:mcp hello")

        payload, failed = self.tool_result(responses[6])
        self.assertTrue(failed)
        self.assertIn("不一致", payload)

        payload, failed = self.tool_result(responses[7])
        self.assertTrue(failed)
        self.assertIn("绝对路径", payload)

        self.use(self.workspace_b)
        responses = self.run_mcp([
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
                "name": "claim_trae_task", "arguments": {"workspace": self.workspace_a}}},
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
                "name": "gemini_a2a_send", "arguments": {
                    "workspace": self.workspace_a, "text": "must not switch"}}},
        ], expected_workspace=self.workspace_a)
        payload, failed = self.tool_result(responses[0])
        self.assertTrue(failed)
        self.assertIn("当前活动工作区", payload)
        payload, failed = self.tool_result(responses[1])
        self.assertTrue(failed)
        self.assertIn("当前活动工作区", payload)
        self.assertEqual(self.health()["workspace"], self.workspace_b)

    def test_managed_send_starts_service_on_demand(self):
        responses = self.run_mcp([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "gemini_a2a_send", "arguments": {
                    "workspace": self.workspace_a, "text": "start on demand"}}},
        ], expected_workspace=self.workspace_a)
        payload, failed = self.tool_result(responses[0])
        self.assertFalse(failed)
        self.assertEqual(payload["text"], "echo:start on demand")
        health = wait_for(self.health)
        self.assertEqual(health["workspace"], self.workspace_a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
