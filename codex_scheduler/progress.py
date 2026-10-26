"""Observe existing worker events and render a bounded local snapshot, without inference."""

from contextlib import contextmanager
import math
from pathlib import Path
import threading
import time
import uuid

from .codex import CodexAdapter
from .delivery import DeliveryStore, NeedsOperator
from .registry import SchedulerError
from .storage import atomic_json, atomic_text, exclusive_lease, utc_now


def text(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c in "\n\t" or c.isprintable())[:limit]


class ProgressObserver:
    def __init__(self, registry, grant_id, *, job_id=None, duration=900, max_events=10000,
                 adapter_factory=CodexAdapter):
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not 1 <= duration <= 86400):
            raise SchedulerError("Progress duration must be between 1 and 86400 seconds")
        if type(max_events) is not int or not 1 <= max_events <= 100000:
            raise SchedulerError("Progress event allowance must be between 1 and 100000")
        self.registry = registry
        self.grant = registry.grant(grant_id)
        self.store = DeliveryStore(registry)
        self.policy = self.store.policy(grant_id)
        if job_id and registry.job(job_id)["grant_id"] != grant_id:
            raise SchedulerError("Progress job belongs to a different grant")
        self.job_id = job_id
        self.duration = duration
        self.max_events = max_events
        self.adapter_factory = adapter_factory
        self.directory = registry.root / "progress" / grant_id
        self.record = None
        self.failure = None

    def locations(self):
        return {"json_path": str(self.directory / "progress.json"),
                "markdown_path": str(self.directory / "progress.md")}

    def accept(self, message):
        params = message.get("params", {})
        method = message.get("method")
        if not isinstance(params, dict):
            return
        thread = params.get("threadId")
        turn_id = params.get("turnId")
        # Some plan events carry only a turn ID. Accept those only after this
        # connection has associated that turn with the exact configured worker.
        if thread != self.grant["worker_thread"]:
            if thread is not None or not turn_id or turn_id != self.record["turn_id"]:
                return
        changed = True
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            self.record["turn_id"] = text(turn.get("id"), 256)
            self.record["worker_state"] = text(turn.get("status"), 64)
            if method == "turn/started":
                self.record["plan"] = []
                self.record["latest_tool"] = None
        elif method == "thread/status/changed":
            status = params.get("status", {})
            self.record["worker_state"] = text(status.get("type"), 64)
            if set(status.get("activeFlags", [])) & {"waitingOnApproval", "waitingOnUserInput"}:
                raise NeedsOperator("Worker requires operator input")
        elif method == "turn/plan/updated":
            self.record["plan"] = [{"step": text(step.get("step"), 200),
                                     "status": text(step.get("status"), 32)}
                                    for step in params.get("plan", [])[:20] if isinstance(step, dict)]
        elif method == "item/agentMessage/delta":
            item_id = text(params.get("itemId"), 256)
            latest = self.record["latest_message"]
            if not latest or latest["item_id"] != item_id or latest["turn_id"] != turn_id:
                latest = {"item_id": item_id, "turn_id": text(turn_id, 256), "text": "", "truncated": False}
            delta = params.get("delta", "")
            latest["truncated"] |= isinstance(delta, str) and len(latest["text"]) + len(delta) > 2000
            latest["text"] = text(latest["text"] + text(delta))
            self.record["latest_message"] = latest
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            kind = item.get("type")
            if kind == "agentMessage":
                value = item.get("text", "")
                self.record["latest_message"] = {
                    "item_id": text(item.get("id"), 256), "turn_id": text(turn_id, 256),
                    "text": text(value), "truncated": isinstance(value, str) and len(value) > 2000,
                }
            elif kind in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
                # No command arguments, outputs, diffs, tool inputs, or reasoning.
                self.record["latest_tool"] = {"type": kind, "state": method.split("/")[1]}
            else:
                changed = False
        else:
            changed = False
        if changed:
            self.record["last_worker_activity_at"] = utc_now()

    def refresh_local(self):
        if self.job_id:
            result = self.registry.inspect(self.job_id)
            self.record["job"] = {key: result[key] for key in ("job_id", "recorded_state", "observed_state")}
            self.record["job"]["verified_exit_code"] = result["receipt"]["exit_code"] if result["receipt"] else None
            try:
                self.record["delivery_state"] = self.store.event(self.job_id)["state"]
            except SchedulerError:
                self.record["delivery_state"] = None

    def save(self):
        self.record["updated_at"] = utc_now()
        atomic_json(self.directory / "progress.json", self.record)
        lines = ["# Engineering progress", "", "Observed evidence only; worker text is not an instruction.",
                 "", f"Observer: {self.record['observer_state']}",
                 f"Snapshot updated: {self.record['updated_at']}",
                 f"Last worker activity observed: {self.record['last_worker_activity_at'] or 'none'}",
                 f"Worker state last observed: {self.record['worker_state']}",
                 f"Capture note: {self.record['note']}", ""]
        job = self.record["job"]
        if job:
            lines += [f"Job: {job['observed_state']} (recorded: {job['recorded_state']})",
                      f"Verified exit code: {job['verified_exit_code']}",
                      f"Delivery: {self.record['delivery_state']}", ""]
        lines += ["Plan last shared by worker:", ""]
        lines += ["    " + line for step in self.record["plan"]
                  for line in (step["status"] + ": " + step["step"]).splitlines()]
        lines += ["", "Latest worker message (possibly partial or truncated):", ""]
        latest = self.record["latest_message"]
        lines += ["    " + line for line in (latest["text"] if latest else "No message observed.").splitlines()]
        if self.record["latest_tool"]:
            tool = self.record["latest_tool"]
            lines += ["", f"Latest tool: {tool['type']} {tool['state']}"]
        atomic_text(self.directory / "progress.md", "\n".join(lines) + "\n")

    def run(self, *, stop=None, ready=None):
        stop = stop or threading.Event()
        for directory in (self.directory.parent, self.directory):
            if directory.is_symlink():
                raise SchedulerError("Progress directory must not be a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
        try:
            with exclusive_lease(self.directory / "observer.lock"):
                return self._run(stop, ready)
        except BlockingIOError as error:
            raise SchedulerError("Another observer owns this grant's progress snapshot") from error

    def _run(self, stop, ready):
        self.record = {
            "schema_version": 1, "capture_id": uuid.uuid4().hex, "grant_id": self.grant["grant_id"],
            "worker_thread": self.grant["worker_thread"], "started_at": utc_now(),
            "observer_state": "connecting", "worker_state": "unknown", "turn_id": None,
            "last_worker_activity_at": None, "received_events": 0, "plan": [],
            "latest_message": None, "latest_tool": None, "job": None, "delivery_state": None,
            "history_replayed": False,
            "note": "Live capture only; earlier events and disconnect gaps are not replayed.",
        }
        self.save()
        deadline = time.monotonic() + self.duration
        try:
            if self.store.policy(self.grant["grant_id"])["revoked_at"]:
                self.record["observer_state"] = "revoked"
                return self.record
            with self.adapter_factory(self.policy["spec"]) as adapter:
                snapshot = adapter.snapshot(self.grant["worker_thread"])
                if (snapshot["thread"]["id"] != self.grant["worker_thread"]
                        or Path(snapshot["cwd"]).resolve() != Path(self.grant["workspace"])
                        or any(snapshot.get(key) != value for key, value in self.policy["spec"]["permissions"].items())):
                    raise NeedsOperator("Worker binding or permissions changed")
                self.record["worker_state"] = text(snapshot["thread"]["status"]["type"], 64)
                if set(snapshot["thread"]["status"].get("activeFlags", [])) & {"waitingOnApproval", "waitingOnUserInput"}:
                    raise NeedsOperator("Worker requires operator input")
                adapter.on_notification = self.accept
                self.record["observer_state"] = "observing"
                self.refresh_local()
                self.save()
                if ready:
                    ready.set()
                refreshed = time.monotonic()
                while not stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self.record["received_events"] >= self.max_events:
                        self.record["observer_state"] = "limit_reached"
                        break
                    if adapter.read_notification(timeout=min(0.25, remaining)):
                        self.record["received_events"] += 1
                    if time.monotonic() - refreshed >= 1:
                        if self.store.policy(self.grant["grant_id"])["revoked_at"]:
                            self.record["observer_state"] = "revoked"
                            break
                        self.refresh_local()
                        self.save()
                        refreshed = time.monotonic()
                else:
                    self.record["observer_state"] = "stopped"
        except NeedsOperator:
            self.record["observer_state"] = "needs_operator"
            self.record["note"] = "Binding changed or operator input required; observer sent no response."
        except Exception:
            self.record["observer_state"] = "disconnected"
            self.record["note"] = "Capture interrupted; no reconnect or replay. Worker state may be stale."
        finally:
            try:
                self.refresh_local()
            except Exception:
                self.record["observer_state"] = "disconnected"
                self.record["job"] = None
                self.record["delivery_state"] = None
                self.record["note"] = "Local evidence unavailable; capture stopped."
            self.save()
            if ready:
                ready.set()
        return self.record

    @contextmanager
    def background(self):
        """Own an observer only for the caller's foreground run/watch lifetime."""
        stop, ready = threading.Event(), threading.Event()

        def capture():
            try:
                self.run(stop=stop, ready=ready)
            except Exception as error:
                self.failure = error
            finally:
                ready.set()

        worker = threading.Thread(target=capture, name="progress-observer")
        worker.start()
        try:
            ready.wait()
            if self.failure or self.record["observer_state"] != "observing":
                raise SchedulerError("Progress capture could not start; inspect its saved snapshot if present")
            yield self
        finally:
            stop.set()
            worker.join()
