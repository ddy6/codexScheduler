"""Progress capture observes evidence without engineering turns or budget changes."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.codex import CodexAdapter
from codex_scheduler.delivery import DEFAULT_PERMISSIONS, DeliveryStore, NeedsOperator
from codex_scheduler.progress import ProgressObserver
from codex_scheduler.runner import run_wrapper
from codex_scheduler.storage import exclusive_lease


def event(method, **params):
    return {"method": method, "params": {"threadId": "synthetic-worker", **params}}


class WorkerStream:
    """Only observation is available; there is deliberately no start/send method."""
    def __init__(self, workspace, events=()):
        self.info = {**DEFAULT_PERMISSIONS, "cwd": str(workspace),
                     "thread": {"id": "synthetic-worker", "status": {"type": "active"}}}
        self.events = iter(events)
        self.on_notification = None
        self.calls = []

    def __call__(self, spec):
        self.calls.append(("connect", spec))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def snapshot(self, thread):
        self.calls.append(("snapshot", thread))
        return self.info

    def read_notification(self, *, timeout):
        try:
            value = next(self.events)
        except StopIteration:
            raise ConnectionError("synthetic stream closed")
        if isinstance(value, Exception):
            raise value
        self.on_notification(value)
        return True


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.registry = Registry(self.root / "state", create=True)
        self.grant = self.registry.create_grant(workspace=self.workspace, worker_thread="synthetic-worker", scope="offline capture fixture")
        self.grant_id = self.grant["grant_id"]
        self.store = DeliveryStore(self.registry)
        self.store.configure(self.grant_id, socket_path=self.root / "fixture.sock")

    def observer(self, events=(), **kwargs):
        worker = WorkerStream(self.workspace, events)
        return ProgressObserver(self.registry, self.grant_id, adapter_factory=worker, **kwargs), worker

    def test_captures_streamed_message_plan_and_tool_without_inference_or_allowance_changes(self):
        events = [event("turn/started", turn={"id": "turn-a", "status": "inProgress"}),
                  {"method": "turn/plan/updated", "params": {"turnId": "turn-a", "plan": [{"step": "Run checks", "status": "inProgress"}]}},
                  event("item/agentMessage/delta", turnId="turn-a", itemId="message-a", delta="Running "),
                  event("item/agentMessage/delta", turnId="turn-a", itemId="message-a", delta="checks."),
                  event("item/completed", turnId="turn-a", item={"id": "message-a", "type": "agentMessage", "text": "Checks finished."}),
                  event("item/completed", item={"type": "commandExecution", "command": "secret-command", "aggregatedOutput": "secret-output"}),
                  event("turn/completed", turn={"id": "turn-a", "status": "completed"})]
        observer, worker = self.observer(events, max_events=len(events))
        before = self.store.policy(self.grant_id)
        result = observer.run()
        saved = json.loads(Path(observer.locations()["json_path"]).read_text())
        markdown = Path(observer.locations()["markdown_path"]).read_text()
        self.assertEqual(saved, result)
        self.assertEqual(result["observer_state"], "limit_reached")
        self.assertEqual(result["worker_state"], "completed")
        self.assertEqual(result["plan"][0]["step"], "Run checks")
        self.assertEqual(result["latest_message"]["text"], "Checks finished.")
        self.assertIn("Checks finished.", markdown)
        self.assertNotIn("secret-command", json.dumps(result) + markdown)
        self.assertNotIn("secret-output", json.dumps(result) + markdown)
        self.assertEqual([call[0] for call in worker.calls], ["connect", "snapshot"])
        self.assertEqual(before, self.store.policy(self.grant_id))
        self.assertEqual(self.registry.grant(self.grant_id)["consumed_jobs"], 0)
        self.assertEqual(Path(observer.locations()["json_path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(observer.locations()["markdown_path"]).stat().st_mode & 0o777, 0o600)

    def test_other_threads_unbound_plans_reasoning_user_text_and_outputs_are_excluded(self):
        events = [event("item/completed", threadId="other-worker", item={"type": "agentMessage", "text": "hidden"}),
                  {"method": "turn/plan/updated", "params": {"turnId": "unbound", "plan": [{"step": "hidden"}]}},
                  event("item/completed", item={"type": "reasoning", "summary": "hidden"}),
                  event("item/completed", item={"type": "userMessage", "text": "hidden"}),
                  event("item/commandExecution/outputDelta", delta="hidden")]
        observer, _ = self.observer(events, max_events=len(events))
        result = observer.run()
        self.assertIsNone(result["latest_message"])
        self.assertIsNone(result["last_worker_activity_at"])
        self.assertEqual(result["plan"], [])
        self.assertNotIn("hidden", json.dumps(result))

    def test_message_and_plan_size_are_bounded_and_markdown_keeps_worker_text_literal(self):
        value = "<script>unsafe</script>\n# not a heading\n" + "x" * 10000
        events = [event("item/completed", item={"id": "message", "type": "agentMessage", "text": value}),
                  event("turn/plan/updated", plan=[{"step": "x" * 1000, "status": "pending"}] * 100)]
        observer, _ = self.observer(events, max_events=2)
        result = observer.run()
        self.assertLessEqual(len(result["latest_message"]["text"]), 2000)
        self.assertTrue(result["latest_message"]["truncated"])
        self.assertEqual(len(result["plan"]), 20)
        self.assertEqual(len(result["plan"][0]["step"]), 200)
        self.assertIn("    <script>unsafe</script>", Path(observer.locations()["markdown_path"]).read_text())
        self.assertLess(Path(observer.locations()["json_path"]).stat().st_size, 15000)

    def test_no_receipt_never_becomes_verified_success_and_real_receipt_is_copied(self):
        job = self.registry.register(self.grant_id, [sys.executable, "-c", "print(45)"])
        observer, _ = self.observer(job_id=job["job_id"])
        result = observer.run()
        self.assertEqual(result["job"]["observed_state"], "registered")
        self.assertIsNone(result["job"]["verified_exit_code"])
        self.registry.reserve_launch(job["job_id"])
        run_wrapper(self.registry, job["job_id"])
        observer, _ = self.observer(job_id=job["job_id"])
        result = observer.run()
        self.assertEqual(result["job"]["observed_state"], "succeeded")
        self.assertEqual(result["job"]["verified_exit_code"], 0)

    def test_mismatched_worker_workspace_and_permissions_stop_before_consuming_events(self):
        for field in ("worker", "workspace", "permissions"):
            with self.subTest(field=field):
                observer, worker = self.observer([event("item/completed", item={"type": "agentMessage", "text": "do not capture"})])
                if field == "worker":
                    worker.info["thread"]["id"] = "other-worker"
                elif field == "workspace":
                    worker.info["cwd"] = str(self.root)
                else:
                    worker.info["approvalPolicy"] = "never"
                result = observer.run()
                self.assertEqual(result["observer_state"], "needs_operator")
                self.assertIsNone(result["latest_message"])
                self.assertEqual(result["received_events"], 0)

    def test_disconnect_and_operator_requests_stop_without_reconnect_or_reply(self):
        for failure, state in ((ConnectionError("hidden"), "disconnected"), (NeedsOperator("hidden"), "needs_operator")):
            with self.subTest(state=state):
                observer, worker = self.observer([failure])
                result = observer.run()
                self.assertEqual(result["observer_state"], state)
                self.assertEqual(len(worker.calls), 2)
                self.assertNotIn("hidden", json.dumps(result))

    def test_revoked_grant_does_not_contact_worker(self):
        self.store.revoke(self.grant_id)
        observer, worker = self.observer()
        self.assertEqual(observer.run()["observer_state"], "revoked")
        self.assertEqual(worker.calls, [])

    def test_second_observer_cannot_overwrite_the_first_snapshot(self):
        observer, _ = self.observer()
        observer.run()
        before = Path(observer.locations()["json_path"]).read_bytes()
        with exclusive_lease(observer.directory / "observer.lock"):
            another, worker = self.observer()
            with self.assertRaises(SchedulerError):
                another.run()
        self.assertEqual(worker.calls, [])
        self.assertEqual(Path(observer.locations()["json_path"]).read_bytes(), before)

    def test_revocation_during_capture_stops_without_touching_delivery_counters(self):
        observer, worker = self.observer()

        def revoke(**kwargs):
            self.store.revoke(self.grant_id)
            return False

        worker.read_notification = revoke
        with patch("codex_scheduler.progress.time.monotonic", side_effect=[0, 0, 0, 1.1]):
            result = observer.run()
        self.assertEqual(result["observer_state"], "revoked")
        policy = self.store.policy(self.grant_id)
        self.assertEqual(policy["consumed_checks"], 0)
        self.assertEqual(policy["consumed_continuations"], 0)

    def test_approval_already_pending_stops_before_listening(self):
        observer, worker = self.observer()
        worker.info["thread"]["status"]["activeFlags"] = ["waitingOnApproval"]
        self.assertEqual(observer.run()["observer_state"], "needs_operator")

    def test_restart_drops_old_messages_and_labels_capture_gap(self):
        observer, _ = self.observer([event("item/completed", item={"id": "message", "type": "agentMessage", "text": "old text"})])
        previous = observer.run()
        observer, _ = self.observer()
        current = observer.run()
        self.assertNotEqual(previous["capture_id"], current["capture_id"])
        self.assertIsNone(current["latest_message"])
        self.assertFalse(current["history_replayed"])

    def test_background_context_stops_and_joins_on_caller_error(self):
        observer, worker = self.observer()
        worker.read_notification = lambda **kwargs: threading.Event().wait(0.001)
        with self.assertRaisesRegex(RuntimeError, "caller stopped"):
            with observer.background():
                raise RuntimeError("caller stopped")
        self.assertEqual(observer.record["observer_state"], "stopped")
        self.assertFalse(any(thread.name == "progress-observer" for thread in threading.enumerate()))

    def test_duration_and_event_limits_are_explicit_and_timeouts_do_not_imply_worker_completion(self):
        for kwargs in ({"duration": 0}, {"duration": float("nan")}, {"max_events": 0}, {"max_events": 100001}):
            with self.assertRaises(SchedulerError):
                self.observer(**kwargs)
        observer, worker = self.observer(duration=1)
        worker.read_notification = lambda **kwargs: False
        with patch("codex_scheduler.progress.time.monotonic", side_effect=[0, 0, 2]):
            result = observer.run()
        self.assertEqual(result["observer_state"], "limit_reached")
        self.assertEqual(result["worker_state"], "active")
        self.assertIsNone(result["last_worker_activity_at"])

    def test_foreign_job_and_symlink_destination_are_refused(self):
        other = self.registry.create_grant(workspace=self.workspace, worker_thread="other", scope="other")
        job = self.registry.register(other["grant_id"], [sys.executable, "-c", "print(1)"])
        with self.assertRaises(SchedulerError):
            self.observer(job_id=job["job_id"])
        observer, worker = self.observer()
        observer.directory.parent.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(SchedulerError):
            observer.run()
        self.assertEqual(worker.calls, [])

    def test_cli_observation_writes_snapshot_without_launching_or_dispatching(self):
        worker = WorkerStream(self.workspace, [event("thread/status/changed", status={"type": "idle"})])
        output = io.StringIO()
        with patch("codex_scheduler.codex.CodexAdapter", worker), redirect_stdout(output):
            result = cli(["--state-dir", str(self.registry.root), "observe-progress", "--grant", self.grant_id, "--max-events", "1"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["observer_state"], "limit_reached")
        self.assertEqual(self.store.policy(self.grant_id)["consumed_checks"], 0)

    def test_run_refuses_to_launch_if_progress_binding_fails(self):
        job = self.registry.register(self.grant_id, [sys.executable, "-c", "print(1)"])
        self.store.arm(job["job_id"], "Report and stop")
        worker = WorkerStream(self.workspace)
        worker.info["cwd"] = str(self.root)
        with patch("codex_scheduler.codex.CodexAdapter", return_value=worker) as factory, redirect_stderr(io.StringIO()):
            result = cli(["--state-dir", str(self.registry.root), "run", "--job", job["job_id"], "--progress"])
        self.assertEqual(result, 2)
        self.assertEqual(self.registry.grant(self.grant_id)["consumed_jobs"], 0)
        factory.require_available.assert_called_once()

    def test_run_captures_progress_and_final_receipt_in_one_foreground_call(self):
        job = self.registry.register(self.grant_id, [sys.executable, "-c", "print(45)"])
        job_id = job["job_id"]
        self.store.arm(job_id, "Report and stop")
        worker = WorkerStream(self.workspace)
        notifications, captured = queue.Queue(), threading.Event()

        def receive(**kwargs):
            try:
                value = notifications.get(timeout=kwargs["timeout"])
            except queue.Empty:
                return False
            worker.on_notification(value)
            captured.set()
            return True

        def launch(registry, selected):
            self.assertEqual(selected, job_id)
            registry.reserve_launch(selected)
            run_wrapper(registry, selected)

        def watch(selected, **kwargs):
            notifications.put(event("item/completed", item={"id": "message", "type": "agentMessage", "text": "Read the receipt; stopping."}))
            if not captured.wait(2):
                raise AssertionError("Observer did not capture the existing worker message")
            return {"state": "completed"}

        worker.read_notification = receive
        output = io.StringIO()
        with patch("codex_scheduler.codex.CodexAdapter", return_value=worker), \
                patch("codex_scheduler.__main__.launch", side_effect=launch), \
                patch("codex_scheduler.delivery.Dispatcher.watch", side_effect=watch), redirect_stdout(output):
            code = cli(["--state-dir", str(self.registry.root), "run", "--job", job_id, "--progress"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["progress"]["observer_state"], "stopped")
        saved = json.loads(Path(result["progress"]["json_path"]).read_text())
        self.assertEqual(saved["latest_message"]["text"], "Read the receipt; stopping.")
        self.assertEqual(saved["job"]["observed_state"], "succeeded")
        self.assertEqual(saved["job"]["verified_exit_code"], 0)
        self.assertEqual(self.registry.grant(self.grant_id)["consumed_jobs"], 1)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 0)


class StreamAdapterTests(unittest.TestCase):
    def test_rpc_interleaving_preserves_notifications_and_resumes_without_overrides(self):
        class Connection:
            def __init__(self):
                self.sent = []
                self.values = iter([event("thread/status/changed", status={"type": "idle"}),
                                    {"id": 1, "result": {"thread": {"id": "synthetic-worker"}}}])

            def send(self, value):
                self.sent.append(json.loads(value))

            def recv(self, **kwargs):
                return json.dumps(next(self.values))

        captured = []
        adapter = CodexAdapter({}, on_notification=captured.append)
        adapter.connection = Connection()
        adapter.snapshot("synthetic-worker")
        self.assertEqual(captured[0]["method"], "thread/status/changed")
        self.assertEqual(adapter.connection.sent, [{"id": 1, "method": "thread/resume", "params": {
            "threadId": "synthetic-worker", "excludeTurns": True}}])

    def test_notification_read_never_sends_and_approval_requests_are_not_answered(self):
        class Connection:
            def __init__(self, values):
                self.values = iter(values)

            def recv(self, **kwargs):
                value = next(self.values)
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value)

        captured = []
        adapter = CodexAdapter({}, on_notification=captured.append)
        notification = event("thread/status/changed", status={"type": "idle"})
        adapter.connection = Connection([TimeoutError(), notification, {"method": "item/commandExecution/requestApproval", "id": 7}])
        self.assertFalse(adapter.read_notification(timeout=0.01))
        self.assertTrue(adapter.read_notification(timeout=0.01))
        self.assertEqual(captured, [notification])
        with self.assertRaises(NeedsOperator):
            adapter.read_notification(timeout=0.01)


if __name__ == "__main__":
    unittest.main()
