"""Codex Scheduler experiment: register a job, yield, and verify worker continuation."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time


class TrialError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """Replace a local receipt atomically; this is not a recovery journal."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Trial:
    def __init__(self, directory, transport, retry_bootstrap=False):
        self.directory = Path(directory).resolve()
        if retry_bootstrap:
            self.state = json.loads((self.directory / "status.json").read_text())
            if (self.state["phase"] not in {"stopped", "preflight_passed"}
                    or self.state.get("stopped_phase") not in {
                        None, "starting_server", "initializing_connection", "preflight_passed"}
                    or self.state.get("thread_id") or self.state.get("initial_turn_id")
                    or self.state["job_attempts"] or self.state["continuation_attempts"]
                    or self.state["transport"] != transport):
                raise TrialError("Only a bootstrap with no worker or consumed allowance can be retried")
            attempts = self.state.setdefault("bootstrap_history", [])
            attempts.append({key: self.state.get(key)
                             for key in ("phase", "stopped_at", "error", "stopped_phase")})
            self.write(phase="prepared", error=None)
            return
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.state = {
            "phase": "prepared",
            "created_at": now(),
            "transport": transport,
            "scope": "one synthetic job in a disposable workspace",
            "sandbox": "read-only",
            "approval_policy": "on-request",
            "max_jobs": 1,
            "job_attempts": 0,
            "max_continuations": 1,
            "continuation_attempts": 0,
            "thread_id": None,
            "job_id": secrets.token_hex(8),
        }
        self.write()

    def write(self, **updates):
        self.state.update(updates)
        save(self.directory / "status.json", self.state)

    def reserve(self, kind):
        key, limit = {
            "job": ("job_attempts", "max_jobs"),
            "continuation": ("continuation_attempts", "max_continuations"),
        }[kind]
        if self.state[key] >= self.state[limit]:
            raise TrialError(f"{kind} allowance exhausted")
        if not self.state["thread_id"]:
            raise TrialError("No exact worker thread has been registered")
        if kind == "continuation" and self.state["phase"] != "job_completed":
            raise TrialError("Completion is not verified")
        self.write(**{key: self.state[key] + 1, "phase": f"{kind}_reserved"})


class Rpc:
    """Small JSON-RPC client for a WebSocket or a test JSONL process."""

    def __init__(self, command, log, handler, timeout, connection=None):
        self.connection = connection
        self.process = None if connection is not None else subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, bufsize=1,
        )
        self.messages = queue.Queue(maxsize=4096)
        self.handler = handler
        self.timeout = timeout
        self.counter = 0
        self.pending = {}
        self.notifications = []
        self.history = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            source = self.connection if self.connection is not None else self.process.stdout
            for line in source:
                self.messages.put_nowait(json.loads(line))
        except Exception as error:
            self.messages.put(error)
        finally:
            self.messages.put(None)

    def send(self, message):
        if self.connection is not None:
            self.connection.send(json.dumps(message))
            return
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrialError("Timed out waiting for the worker")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as error:
            raise TrialError("Timed out waiting for the worker") from error
        if message is None:
            raise TrialError("App-server connection closed")
        if isinstance(message, Exception):
            raise TrialError("Invalid app-server output") from message
        if "method" in message and "id" in message:
            self.handler(self, message)
        elif "id" in message:
            self.pending[message["id"]] = message
        else:
            self.notifications.append(message)
            self.history.append(event_summary(message))
        return message

    def call(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while request_id not in self.pending:
            try:
                self.receive(deadline)
            except TrialError as error:
                raise TrialError(f"{method}: {error}") from error
        response = self.pending.pop(request_id)
        if "error" in response:
            raise TrialError(f"{method} failed: {response['error']}")
        return response["result"]

    def completed_turn(self, thread_id, turn_id=None):
        deadline = time.monotonic() + self.timeout
        while True:
            for index, event in enumerate(self.notifications):
                params = event.get("params", {})
                if (event.get("method") == "turn/completed"
                        and params.get("threadId") == thread_id
                        and (turn_id is None or params["turn"]["id"] == turn_id)):
                    self.notifications.pop(index)
                    turn = params["turn"]
                    if turn["status"] != "completed":
                        raise TrialError(f"Worker turn ended as {turn['status']}")
                    return turn
            self.receive(deadline)

    def close(self):
        if self.connection is not None:
            self.connection.close()
        else:
            stop(self.process)
            self.process.stdin.close()
            self.process.stdout.close()
        self.reader.join(timeout=1)


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def input_text(text):
    return [{"type": "text", "text": text}]


def event_summary(event):
    """Store lifecycle evidence, never authentication replies or raw tool data."""
    params = event.get("params", {})
    item = params.get("item", {})
    turn = params.get("turn", {})
    return {
        "method": event.get("method"),
        "thread_id": params.get("threadId"),
        "turn_id": params.get("turnId", turn.get("id")),
        "item_type": item.get("type"),
        "status": turn.get("status"),
    }


def verify_completion(entries, expected):
    items = [entry["item"] for entry in entries]
    allowed = {"agentMessage", "userMessage", "reasoning"}
    if any(item.get("type") not in allowed for item in items):
        raise TrialError("Unexpected tool use during the completion turn")
    messages = [item["text"].strip() for item in items
                if item.get("type") == "agentMessage"]
    if expected not in messages:
        raise TrialError("Worker did not demonstrate retained context and job identity")


class Harness:
    def __init__(self, trial, codex, timeout, model=None):
        self.trial = trial
        self.codex = codex
        self.timeout = timeout
        self.model = model
        self.job = None
        self.job_log = None
        self.initial_turn = None
        self.token = secrets.token_hex(12)

    def handle_request(self, rpc, request):
        params = request.get("params", {})
        if (request["method"] == "item/tool/call"
                and params.get("tool") == "start_synthetic_job"
                and params.get("threadId") == self.trial.state["thread_id"]
                and params.get("turnId") == self.initial_turn
                and params.get("arguments") == {}):
            self.start_job()
            rpc.send({"id": request["id"], "result": {
                "success": True,
                "contentItems": [{"type": "inputText", "text": json.dumps({
                    "status": "running", "job_id": self.trial.state["job_id"],
                })}],
            }})
            return
        # An unattended trial never supplies an approval or guesses an answer.
        self.trial.write(phase="needs_operator", blocked_request=request["method"])
        rpc.send({"id": request["id"], "error": {
            "code": -32000, "message": "Trial stopped: operator input required",
        }})
        raise TrialError(f"Operator input required for {request['method']}")

    def before_initial_turn(self, configuration):
        pass

    def start_job(self):
        self.trial.reserve("job")
        self.job_log = (self.trial.directory / "job.log").open("w")
        command = [
            sys.executable, str(Path(__file__).with_name("synthetic_job.py")),
            "--release", str(self.trial.directory / "release"), "--timeout", str(self.timeout),
        ]
        self.trial.write(job_command=command, job_started_at=now())
        self.job = subprocess.Popen(command, cwd=self.trial.directory / "workspace",
                                    stdout=self.job_log, stderr=subprocess.STDOUT)
        self.trial.write(phase="job_running", job_pid=self.job.pid)

    def finish_job(self):
        code = self.job.wait(timeout=self.timeout)
        self.job_log.close()
        output = (self.trial.directory / "job.log").read_text()
        if code != 0 or len(output) > 1024:
            raise TrialError("Synthetic job failed; no continuation sent")
        if json.loads(output) != {"result": 45, "status": "completed"}:
            raise TrialError("Synthetic job produced unexpected evidence")
        receipt = {"job_id": self.trial.state["job_id"], "exit_code": code,
                   "result": 45, "completed_at": now()}
        save(self.trial.directory / "completion.json", receipt)
        return receipt

    def deliver_completion(self, rpc, thread_id, receipt):
        completion = json.dumps({"event": "job_completed", **receipt})
        self.trial.reserve("continuation")
        self.trial.write(completion_prompt=completion, dispatch_started_at=now())
        if self.trial.state["transport"] == "queue":
            result = subprocess.run(
                [self.codex, "queue", "--remote", f"unix://{self.socket_path}",
                 "--thread", thread_id, "--message", completion],
                cwd=self.trial.directory / "workspace", capture_output=True, text=True, timeout=20,
            )
            (self.trial.directory / "queue.log").write_text(result.stdout + result.stderr)
            if result.returncode:
                raise TrialError("Queue delivery failed or is uncertain; inspect queue.log")
            self.trial.write(phase="queue_accepted")
            return rpc.completed_turn(thread_id)
        result = rpc.call("turn/start", {"threadId": thread_id, "input": input_text(completion)})
        return rpc.completed_turn(thread_id, result["turn"]["id"])

    def cleanup_job(self):
        stop(self.job)
        if self.job_log and not self.job_log.closed:
            self.job_log.close()

    def run(self, preflight_only=False):
        from websockets.sync.client import unix_connect

        server = rpc = None
        directory = self.trial.directory
        workspace = directory / "workspace"
        workspace.mkdir(exist_ok=True)
        initial_prompt = (
            f"This is a bounded worker lifecycle trial. Remember context token {self.token}. "
            "Call start_synthetic_job exactly once with no arguments. "
            "When it returns running, immediately reply WAITING and end your turn. "
            "Do not wait, check progress, or use any other tools. "
            "On a later job completion event, reply exactly "
            "CONTINUED:<remembered context token>:<job_id>:<exit_code>, then stop. "
            "Do not call tools on that later turn."
        )
        self.trial.write(initial_prompt=initial_prompt, phase="starting_server")
        with tempfile.TemporaryDirectory(prefix="sr-", dir="/tmp") as socket_dir:
            socket_path = Path(socket_dir) / "server.sock"
            self.socket_path = socket_path
            with (directory / "server.log").open("a") as server_log:
                try:
                    command = [self.codex, "app-server", "--listen", f"unix://{socket_path}"]
                    # Reduce this synthetic worker's tools without widening permissions.
                    for key in ("features.shell_tool", "features.apps", "features.plugins",
                                "features.memories", "agents.enabled"):
                        command.extend(["-c", f"{key}=false"])
                    server = subprocess.Popen(
                        command, cwd=workspace, stdout=server_log, stderr=server_log,
                    )
                    deadline = time.monotonic() + 15
                    while not socket_path.exists():
                        if server.poll() is not None or time.monotonic() >= deadline:
                            raise TrialError("Local server did not open its socket; inspect server.log")
                        time.sleep(0.05)
                    self.trial.write(phase="initializing_connection")
                    connection = unix_connect(str(socket_path), open_timeout=5, close_timeout=1)
                    rpc = Rpc(None, None, self.handle_request, 5, connection=connection)
                    rpc.call("initialize", {
                        "clientInfo": {"name": "codex_scheduler_trial", "version": "0.1"},
                        "capabilities": {"experimentalApi": True},
                    })
                    rpc.send({"method": "initialized", "params": {}})
                    self.trial.write(phase="preflight_passed")
                    if preflight_only:
                        return
                    rpc.timeout = self.timeout
                    params = {
                        "cwd": str(workspace), "sandbox": "read-only",
                        "approvalPolicy": "on-request", "approvalsReviewer": "user",
                        "ephemeral": False,
                        "developerInstructions": (
                            "Perform only the synthetic trial requested. Use only the "
                            "start_synthetic_job tool, once, on the initial turn."
                        ),
                        "dynamicTools": [{
                            "type": "function", "name": "start_synthetic_job",
                            "description": "Register and start the one approved harmless job; returns running.",
                            "inputSchema": {"type": "object", "properties": {},
                                            "required": [], "additionalProperties": False},
                        }],
                    }
                    if self.model:
                        params["model"] = self.model
                    self.trial.write(phase="creating_worker")
                    result = rpc.call("thread/start", params)
                    thread_id = result["thread"]["id"]
                    if result["sandbox"]["type"] != "readOnly" or result["approvalPolicy"] != "on-request":
                        raise TrialError("Server did not retain the requested permission settings")
                    if result["thread"].get("canAcceptDirectInput") is False:
                        raise TrialError("Worker does not accept direct input")
                    self.trial.write(
                        thread_id=thread_id, model=result["model"],
                        effective_sandbox=result["sandbox"],
                        effective_approval_policy=result["approvalPolicy"],
                        phase="starting_initial_turn",
                    )
                    self.before_initial_turn(result)
                    result = rpc.call("turn/start", {
                        "threadId": thread_id, "input": input_text(initial_prompt),
                    })
                    self.initial_turn = result["turn"]["id"]
                    self.trial.write(initial_turn_id=self.initial_turn)
                    rpc.completed_turn(thread_id, self.initial_turn)
                    if self.job is None or self.job.poll() is not None:
                        raise TrialError("The job did not remain running beyond the initial turn")
                    self.trial.write(phase="worker_yielded", initial_turn_completed_at=now(),
                                     job_survived_turn=True)
                    (directory / "release").touch(exist_ok=False)
                    receipt = self.finish_job()
                    self.trial.write(phase="job_completed")
                    completed = self.deliver_completion(rpc, thread_id, receipt)
                    items = rpc.call("thread/items/list", {
                        "threadId": thread_id, "turnId": completed["id"], "limit": 100,
                    })
                    if items.get("nextCursor"):
                        raise TrialError("Unexpectedly large completion turn")
                    expected = f"CONTINUED:{self.token}:{receipt['job_id']}:{receipt['exit_code']}"
                    verify_completion(items["data"], expected)
                    metadata = rpc.call("thread/read", {"threadId": thread_id})["thread"]
                    if metadata["status"]["type"] != "idle":
                        raise TrialError("Worker did not return to idle")
                    turns = rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 10})
                    if turns.get("nextCursor") or len(turns["data"]) != 2:
                        raise TrialError("Expected exactly an initial turn and one continuation")
                    self.trial.write(
                        phase="passed", completed_at=now(), continuation_turn_id=completed["id"],
                        context_retained=True, turn_count=2, worker_idle=True,
                        native_child_tested=False, restart_tested=False,
                    )
                finally:
                    if rpc:
                        save(directory / "events.json", rpc.history)
                        rpc.close()
                    stop(server)
                    self.cleanup_job()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="New private directory; existing directories are refused")
    parser.add_argument("--transport", choices=("queue", "turn-start"), default="queue")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", help="Omit to use the configured model")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Check the transport without creating a worker or invoking a model")
    parser.add_argument("--retry-bootstrap", action="store_true",
                        help="Reuse the same grant only if no worker or consumed allowance exists")
    parser.add_argument("--timeout", type=float, default=120,
                        help="Finite timeout for each worker turn or job, in seconds")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    trial = Trial(args.run_dir, args.transport, args.retry_bootstrap)
    try:
        version = subprocess.run([args.codex, "--version"], capture_output=True,
                                 text=True, check=True, timeout=10).stdout.strip()
        trial.write(codex_version=version)
        Harness(trial, args.codex, args.timeout, args.model).run(args.preflight_only)
    except Exception as error:
        trial.write(stopped_phase=trial.state["phase"], phase="stopped",
                    stopped_at=now(), error=str(error))
        print(f"Trial stopped. Inspect {trial.directory / 'status.json'}", file=sys.stderr)
        return 1
    label = "Preflight" if args.preflight_only else "Trial"
    print(f"{label} passed. Inspect {trial.directory / 'status.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
