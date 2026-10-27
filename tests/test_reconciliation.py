"""Large synthetic wire responses must not turn confirmation into another send."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.codex import CodexAdapter
from codex_scheduler.delivery import DEFAULT_PERMISSIONS, DeliveryStore, Dispatcher, NeedsOperator
from codex_scheduler.progress import ProgressObserver
from codex_scheduler.runner import run_wrapper
from codex_scheduler.transport import DEFAULT_RECEIVE_BYTES, MAX_RECEIVE_BYTES, ResponseTooLarge, receive_limit
from codex_scheduler.usage import read_quota


MIB = 1024 * 1024


class LocalSizeClose(Exception):
    sent = SimpleNamespace(code=1009)
    rcvd = None


class HistoryServer:
    """The RPC/JSON boundary is real; model turns and WebSocket IO are simulated."""
    def __init__(self, workspace, count=120):
        self.workspace = workspace
        self.requests = []
        self.starts = []
        self.connections = []
        self.response_sizes = []
        self.pad_snapshot = ""
        self.pad_start = ""
        self.output = "synthetic command output\n" * (3 * MIB // 25)
        old_log = "synthetic historical log\n" * (MIB // 25)
        self.history = [self.turn(f"historical-{i}", "unrelated input", old_log) for i in range(count)]
        self.target = None

    def turn(self, turn_id, request, output=""):
        return {"id": turn_id, "status": "completed", "items": [
            {"id": turn_id + "-user", "type": "userMessage", "content": [{"type": "text", "text": request}]},
            {"id": turn_id + "-command", "type": "commandExecution", "command": "synthetic-check",
             "cwd": str(self.workspace), "status": "completed", "commandActions": [], "aggregatedOutput": output},
            {"id": turn_id + "-answer", "type": "agentMessage", "text": "Synthetic result."}]}

    def connect(self, path, **kwargs):
        server = self

        class Connection:
            def __init__(self):
                self.max_size = kwargs["max_size"]
                self.responses = []

            def send(self, value):
                request = json.loads(value)
                if "id" in request:
                    response = {"id": request["id"], "result": server.rpc(request["method"], request["params"])}
                    self.responses.append(json.dumps(response))

            def recv(self, **unused):
                response = self.responses.pop(0)
                size = len(response.encode("utf-8"))
                server.response_sizes.append(size)
                if size > self.max_size:
                    raise LocalSizeClose("private protocol close reason")
                return response

            def close(self):
                pass

        connection = Connection()
        self.connections.append(connection)
        return connection

    def rpc(self, method, params):
        self.requests.append((method, dict(params)))
        if method == "initialize":
            return {}
        if method == "thread/resume":
            active = self.target and self.target["status"] == "inProgress"
            return {**DEFAULT_PERMISSIONS, "cwd": str(self.workspace),
                    "thread": {"id": "synthetic-worker", "status": {"type": "active" if active else "idle"}},
                    "synthetic_metadata": self.pad_snapshot}
        if method == "thread/queue/list":
            return {"data": []}
        if method == "turn/start":
            self.starts.append(dict(params))
            turn_id = "acknowledged-turn" if len(self.starts) == 1 else f"acknowledged-turn-{len(self.starts)}"
            self.target = self.turn(turn_id, params["input"][0]["text"], self.output)
            self.target["status"] = "inProgress"
            self.history.append(self.target)
            return {"turn": {"id": self.target["id"], "status": "inProgress", "items": []}, "padding": self.pad_start}
        if method == "thread/turns/list":
            if params["itemsView"] != "notLoaded":
                raise AssertionError("Unrelated full history must never be requested")
            offset = int(params.get("cursor", "turns:0").split(":")[1])
            turns = list(reversed(self.history))
            selected = turns[offset:offset + params["limit"]]
            next_offset = offset + len(selected)
            return {"data": [{"id": t["id"], "status": t["status"], "itemsView": "notLoaded", "items": [],
                              "error": t.get("error")} for t in selected],
                    "nextCursor": f"turns:{next_offset}" if next_offset < len(turns) else None}
        if method == "thread/items/list":
            turn = next(t for t in self.history if t["id"] == params["turnId"])
            offset = int(params.get("cursor", "items:0").split(":")[1])
            items = turn["items"][offset:offset + params["limit"]]
            next_offset = offset + len(items)
            return {"data": [{"turnId": turn["id"], "item": item} for item in items],
                    "nextCursor": f"items:{next_offset}" if next_offset < len(turn["items"]) else None}
        if method == "account/rateLimits/read":
            return {"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 2000000000}}}
        raise AssertionError("Unexpected RPC: " + method)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = Registry(self.root / "state", create=True)
        self.grant = self.registry.create_grant(workspace=self.root, worker_thread="synthetic-worker", scope="one synthetic check")
        self.grant_id = self.grant["grant_id"]
        self.store = DeliveryStore(self.registry)
        self.store.configure(self.grant_id, socket_path=self.root / "synthetic.sock", max_checks=20,
                             max_continuations=3, max_usage_resumes=1)
        self.job_id = self.registry.register(self.grant_id, [sys.executable, "-c", "print(45)"])["job_id"]
        self.store.arm(self.job_id, "Read the receipt and stop.")
        self.registry.reserve_launch(self.job_id)
        run_wrapper(self.registry, self.job_id)
        self.server = HistoryServer(self.root)
        self.connector = patch.object(CodexAdapter, "require_available", return_value=self.server.connect)
        self.connector.start()
        self.addCleanup(self.connector.stop)
        self.dispatcher = Dispatcher(self.registry, CodexAdapter)

    def start(self):
        result = self.dispatcher.step(self.job_id)
        self.assertEqual(result["state"], "started")
        self.server.target["status"] = "completed"
        return result

    def assert_no_extra_attempt(self, checks):
        policy = self.store.policy(self.grant_id)
        self.assertEqual(policy["consumed_continuations"], 1)
        self.assertEqual(policy["consumed_usage_resumes"], 0)
        self.assertEqual(policy["consumed_checks"], checks)
        self.assertEqual(self.registry.grant(self.grant_id)["consumed_jobs"], 1)
        self.assertEqual(len(self.server.starts), 1)
        self.assertEqual(self.store.event(self.job_id)["attempts"], 1)

    def test_acknowledged_turn_ignores_over_100_mib_of_unrelated_tool_history(self):
        self.assertGreater(sum(len(t["items"][1]["aggregatedOutput"]) for t in self.server.history), 100 * MIB)
        self.start()
        result = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(result["state"], "completed")
        item_requests = [p for method, p in self.server.requests if method == "thread/items/list"]
        self.assertEqual([p["turnId"] for p in item_requests], ["acknowledged-turn"])
        self.assertTrue(all(p["itemsView"] == "notLoaded" for method, p in self.server.requests if method == "thread/turns/list"))
        self.assertLess(max(self.server.response_sizes), DEFAULT_RECEIVE_BYTES)
        self.assertTrue(all(c.max_size == 8 * MIB for c in self.server.connections))
        for _ in range(2):
            self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assert_no_extra_attempt(checks=2)

    def test_known_turn_does_not_select_a_newer_turn_or_load_its_output(self):
        self.start()
        for i in range(35):
            self.server.history.append(self.server.turn(f"newer-{i}", self.store.event(self.job_id)["request_text"], "x" * MIB))
        result = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turn_id"], "acknowledged-turn")
        self.assertEqual([p["turnId"] for method, p in self.server.requests if method == "thread/items/list"], ["acknowledged-turn"])
        self.assert_no_extra_attempt(checks=2)

    def test_oversized_confirmation_keeps_acknowledgement_and_runtime_override_preserves_policy(self):
        self.server.output = "synthetic output\n" * (10 * MIB // 17)
        saved = self.start()
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(failed["state"], "uncertain")
        self.assertEqual(failed["turn_id"], saved["turn_id"])
        self.assertIn("completion_confirmation failed: response_too_large", failed["reason"])
        self.assertIn(str(8 * MIB), failed["reason"])
        self.assertNotIn("private protocol", failed["reason"])
        self.assert_no_extra_attempt(checks=2)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli(["--state-dir", str(self.registry.root), "--max-receive-bytes", str(16 * MIB),
                        "reconcile-delivery", "--job", self.job_id])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "completed")
        self.assertEqual(self.server.connections[-1].max_size, 16 * MIB)
        self.assertEqual(self.store.policy(self.grant_id)["spec"]["max_receive_bytes"], 8 * MIB)
        self.assert_no_extra_attempt(checks=3)

    def test_oversized_worker_verification_never_reserves_or_sends(self):
        self.server.pad_snapshot = "x" * (9 * MIB)
        failed = self.dispatcher.step(self.job_id)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn("worker_verification failed: response_too_large", failed["reason"])
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 0)
        self.assertEqual(failed["attempts"], 0)
        self.assertEqual(self.server.starts, [])
        self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_checks"], 1)

    def test_oversized_acknowledgement_uses_bounded_scoped_discovery_without_resending(self):
        self.server.history = self.server.history[:4]
        self.server.pad_start = "x" * (9 * MIB)
        failed = self.dispatcher.step(self.job_id)
        self.assertEqual(failed["state"], "uncertain")
        self.assertIsNone(failed["turn_id"])
        self.assertIn("dispatch_acknowledgement failed: response_too_large", failed["reason"])
        self.server.target["status"] = "completed"
        result = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turn_id"], "acknowledged-turn")
        unrelated = [p for method, p in self.server.requests if method == "thread/items/list" and p["turnId"] != "acknowledged-turn"]
        self.assertTrue(unrelated)
        self.assertTrue(all(p["limit"] == 1 for p in unrelated))
        self.assert_no_extra_attempt(checks=2)

    def test_wrong_input_and_steering_in_later_item_pages_cannot_confirm(self):
        self.start()
        self.server.target["items"] = [self.server.target["items"][0]] + [
            {"id": f"tool-{i}", "type": "commandExecution", "aggregatedOutput": "synthetic log"} for i in range(23)]
        self.server.target["items"].append({"id": "steering", "type": "userMessage", "content": [{"type": "text", "text": "another instruction"}]})
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn("mixed", failed["reason"])
        self.assert_no_extra_attempt(checks=2)

    def test_acknowledged_turn_with_different_input_cannot_be_confirmed(self):
        self.start()
        self.server.target["items"][0]["content"] = [{"type": "text", "text": "different request"}]
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn("input disagrees", failed["reason"])
        self.assert_no_extra_attempt(checks=2)

    def test_verified_input_does_not_turn_an_active_status_into_completion(self):
        self.dispatcher.step(self.job_id)
        active = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(active["state"], "started")
        self.assert_no_extra_attempt(checks=2)
        self.server.target["status"] = "completed"
        self.assertEqual(self.dispatcher.step(self.job_id, reconcile_only=True)["state"], "completed")
        self.assert_no_extra_attempt(checks=3)

    def prepare_usage_resume(self):
        self.start()
        self.server.target["status"] = "failed"
        self.server.target["error"] = {"codexErrorInfo": "usageLimitExceeded"}
        self.assertEqual(self.dispatcher.step(self.job_id)["reason"], "blocked_on_usage")
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "started")
        self.server.target["status"] = "completed"

    def test_usage_resume_confirmation_targets_its_own_acknowledged_turn(self):
        self.prepare_usage_resume()
        result = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["turn_id"], "acknowledged-turn")
        self.assertEqual(result["usage"]["resume"]["turn_id"], "acknowledged-turn-2")
        self.assertEqual([p["turnId"] for method, p in self.server.requests if method == "thread/items/list"],
                         ["acknowledged-turn", "acknowledged-turn-2"])
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 2)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 1)
        self.assertEqual(len(self.server.starts), 2)

    def test_oversized_recovery_preflight_is_a_verification_failure_with_no_new_attempt(self):
        self.start()
        self.server.target["status"] = "failed"
        self.server.target["error"] = {"codexErrorInfo": "usageLimitExceeded"}
        self.dispatcher.step(self.job_id)
        self.server.pad_snapshot = "x" * (9 * MIB)
        failed = self.dispatcher.step(self.job_id)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn("worker_verification failed: response_too_large", failed["reason"])
        self.assert_no_extra_attempt(checks=3)

    def test_oversized_usage_resume_confirmation_does_not_consume_another_resume(self):
        self.prepare_usage_resume()
        self.server.target["items"][1]["aggregatedOutput"] = "x" * (9 * MIB)
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(failed["state"], "uncertain")
        self.assertIn("completion_confirmation failed: response_too_large", failed["reason"])
        self.assertEqual(failed["usage"]["resume"]["turn_id"], "acknowledged-turn-2")
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 2)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 1)
        self.assertEqual(len(self.server.starts), 2)

    def test_missing_turn_and_unbounded_item_history_remain_unresolved(self):
        self.start()
        target = self.server.history.pop()
        missing = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertNotEqual(missing["state"], "completed")
        self.server.history.append(target)
        target["items"] = [target["items"][0]] + [{"id": f"tool-{i}", "type": "agentMessage", "text": "synthetic"} for i in range(110)]
        large = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(large["state"], "blocked")
        self.assertIn("bounded item pages", large["reason"])
        self.assert_no_extra_attempt(checks=3)

    def test_unknown_acknowledgement_never_resends_when_discovery_budget_is_exhausted(self):
        self.server.pad_start = "x" * (9 * MIB)
        self.dispatcher.step(self.job_id)
        self.server.target["status"] = "completed"
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertNotEqual(failed["state"], "completed")
        self.assertIn("bounded reads", failed["reason"])
        self.assert_no_extra_attempt(checks=2)

    def test_lost_acknowledgement_with_duplicate_saved_input_remains_uncertain(self):
        self.server.history = self.server.history[:4]
        self.server.pad_start = "x" * (9 * MIB)
        self.dispatcher.step(self.job_id)
        self.server.target["status"] = "completed"
        self.server.history.append(self.server.turn("duplicate", self.store.event(self.job_id)["request_text"]))
        self.assertEqual(self.dispatcher.step(self.job_id, reconcile_only=True)["state"], "uncertain")
        self.assert_no_extra_attempt(checks=2)

    def test_unsupported_scoped_items_never_fall_back_to_unrelated_full_history(self):
        self.start()
        rpc = self.server.rpc

        def unsupported(method, params):
            if method == "thread/items/list":
                raise SchedulerError("unsupported scoped history")
            return rpc(method, params)

        self.server.rpc = unsupported
        failed = self.dispatcher.step(self.job_id, reconcile_only=True)
        self.assertEqual(failed["state"], "blocked")
        self.assertIn("evidence unavailable from thread/items/list", failed["reason"])
        self.assert_no_extra_attempt(checks=2)

    def test_progress_capture_reports_oversize_instead_of_claiming_worker_completion(self):
        self.server.pad_snapshot = "x" * (9 * MIB)
        observer = ProgressObserver(self.registry, self.grant_id)
        result = observer.run()
        self.assertEqual(result["observer_state"], "response_too_large")
        self.assertEqual(result["failure"]["phase"], "progress_capture")
        self.assertEqual(result["failure"]["max_receive_bytes"], 8 * MIB)
        self.assertNotEqual(result["task_state"], "worker_turn_completed")
        self.assertEqual(self.server.starts, [])


class ReceiveLimitTests(unittest.TestCase):
    def test_bounds_are_finite_integer_bytes_with_an_eight_mib_default(self):
        self.assertEqual(CodexAdapter({}).max_receive_bytes, 8 * MIB)
        for value in (None, 0, True, 1023, 1.5, MAX_RECEIVE_BYTES + 1):
            with self.subTest(value=value), self.assertRaises(SchedulerError):
                receive_limit(value)
        self.assertEqual(receive_limit(MAX_RECEIVE_BYTES), MAX_RECEIVE_BYTES)

    def test_limit_counts_utf8_bytes_and_does_not_parse_oversized_json(self):
        adapter = CodexAdapter({"max_receive_bytes": 1024})
        raw = json.dumps({"id": 1, "result": "\u2603" * 400}, ensure_ascii=False)
        self.assertLess(len(raw), 1024)
        self.assertGreater(len(raw.encode("utf-8")), 1024)
        adapter.connection = SimpleNamespace(send=lambda value: None, recv=lambda **kwargs: raw)
        with self.assertRaises(ResponseTooLarge) as caught:
            adapter.call("thread/items/list", {})
        self.assertEqual(caught.exception.method, "thread/items/list")
        self.assertEqual(caught.exception.limit, 1024)

    def test_peer_rejection_is_not_misreported_as_a_local_receive_overflow(self):
        for sent in (None, SimpleNamespace(code=1009)):
            error = RuntimeError("peer rejected an outgoing request")
            error.sent = sent
            error.rcvd = SimpleNamespace(code=1009)
            error.rcvd_then_sent = True
            adapter = CodexAdapter({})
            adapter.connection = SimpleNamespace(send=lambda value: None, recv=Mock(side_effect=error))
            with self.assertRaises(RuntimeError) as caught:
                adapter.call("thread/resume", {})
            self.assertIs(caught.exception, error)

    def test_oversized_quota_response_propagates_instead_of_becoming_unknown(self):
        adapter = SimpleNamespace(rate_limits=Mock(side_effect=ResponseTooLarge("account/rateLimits/read", 8 * MIB)))
        with self.assertRaises(ResponseTooLarge):
            read_quota(adapter)

    def test_invalid_runtime_override_is_rejected_before_state_or_job_effects(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            state = Path(directory) / "state"
            self.assertEqual(cli(["--state-dir", str(state), "--max-receive-bytes", "0", "upgrade"]), 2)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
