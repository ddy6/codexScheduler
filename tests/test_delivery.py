"""Delivery invariants using real local receipts and a worker without inference."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.codex import CodexAdapter
from codex_scheduler.delivery import DEFAULT_PERMISSIONS, DeliveryStore, Dispatcher, NeedsOperator
from codex_scheduler.runner import run_wrapper
from codex_scheduler.storage import atomic_json, exclusive_lease


class FakeWorker:
    def __init__(self, workspace):
        self.info = {**json.loads(json.dumps(DEFAULT_PERMISSIONS)), "cwd": str(workspace),
                     "thread": {"id": "synthetic-worker", "canAcceptDirectInput": True,
                                "status": {"type": "idle"}}}
        self.starts = []
        self.history = []
        self.queued = False
        self.error_after_send = False
        self.approval = False
        self.on_send = None

    def __call__(self, spec):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def snapshot(self, thread):
        if self.approval:
            raise NeedsOperator("Worker requires operator approval")
        return self.info

    def has_queued_input(self, thread):
        return self.queued

    def start(self, event):
        if self.on_send:
            self.on_send(event)
        self.starts.append(dict(event))
        self.history.append({"id": "synthetic-turn", "status": "inProgress"})
        self.info["thread"]["status"] = {"type": "active", "activeFlags": []}
        if self.error_after_send:
            raise TimeoutError("Acknowledgement lost")
        return "synthetic-turn"

    def find_event(self, event):
        return self.history

    def finish(self, status="completed"):
        self.history[0]["status"] = status
        self.info["thread"]["status"] = {"type": "idle"}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.registry = Registry(self.root / "state", create=True)
        self.store = DeliveryStore(self.registry)
        self.worker = FakeWorker(self.workspace)
        self.dispatcher = Dispatcher(self.registry, self.worker)

    def prepare(self, *, complete=True, continuations=2, checks=20):
        self.grant = self.registry.create_grant(
            workspace=self.workspace, worker_thread="synthetic-worker", scope="one offline fixture", max_jobs=3,
        )
        self.store.configure(self.grant["grant_id"], socket_path=self.root / "worker.sock",
                             max_continuations=continuations, max_checks=checks)
        self.job = self.registry.register(self.grant["grant_id"], [sys.executable, "-c", "print(45)"])
        self.job_id = self.job["job_id"]
        self.store.arm(self.job_id, "Read the receipt and finish the approved fixture.")
        if complete:
            self.registry.reserve_launch(self.job_id)
            run_wrapper(self.registry, self.job_id)

    def policy(self):
        return self.store.policy(self.grant["grant_id"])

    def test_normal_delivery_reserves_before_send_and_stops_at_completion(self):
        self.prepare()

        def check_reserved(event):
            saved = DeliveryStore(Registry(self.registry.root)).event(self.job_id)
            self.assertEqual(saved["state"], "dispatching")
            self.assertEqual(saved["attempts"], 1)
            self.assertEqual(self.policy()["consumed_continuations"], 1)

        self.worker.on_send = check_reserved
        first = self.dispatcher.step(self.job_id)
        self.assertEqual(first["state"], "started")
        self.worker.finish()
        final = self.dispatcher.step(self.job_id)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["event_id"], first["event_id"])
        self.assertEqual(len(self.worker.starts), 1)
        payload = json.loads(final["request_text"])
        self.assertEqual(payload["job_id"], self.job_id)
        self.assertEqual(payload["event_id"], final["event_id"])
        self.assertNotIn("output", payload)
        self.assertNotIn("launch_token", payload)

    def test_duplicate_arm_and_completed_ticks_never_create_another_send(self):
        self.prepare()
        with self.assertRaises(SchedulerError):
            self.store.arm(self.job_id, "duplicate")
        self.dispatcher.step(self.job_id)
        self.worker.finish()
        self.dispatcher.step(self.job_id)
        for _ in range(3):
            self.dispatcher.step(self.job_id)
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.policy()["consumed_checks"], 2)

    def test_lost_acknowledgement_reconciles_from_history_without_resending(self):
        self.prepare()
        self.worker.error_after_send = True
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "uncertain")
        self.worker.finish()
        restarted = Dispatcher(Registry(self.registry.root), self.worker)
        self.assertEqual(restarted.step(self.job_id, reconcile_only=True)["state"], "completed")
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.policy()["consumed_continuations"], 1)

    def test_crash_before_send_keeps_allowance_reserved_and_does_not_guess_a_retry(self):
        self.prepare()
        self.store.reserve(self.job_id)
        result = Dispatcher(Registry(self.registry.root), self.worker).step(self.job_id)
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.worker.starts, [])
        self.assertEqual(self.policy()["consumed_continuations"], 1)

    def test_crash_after_send_before_recording_ack_is_reconciled(self):
        self.prepare()
        event = self.store.reserve(self.job_id)
        self.worker.start(event)
        self.worker.finish()
        result = Dispatcher(Registry(self.registry.root), self.worker).step(self.job_id)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(self.worker.starts), 1)

    def test_unknown_receipt_does_not_wake_a_worker(self):
        self.prepare()
        (self.registry.job_directory(self.job_id) / "completion.json").unlink()
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
        self.assertEqual(self.policy()["consumed_continuations"], 0)
        self.assertEqual(self.worker.starts, [])

    def test_registered_and_starting_jobs_are_only_checked(self):
        self.prepare(complete=False)
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "pending")
        self.registry.reserve_launch(self.job_id)
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "pending")
        self.assertEqual(self.worker.starts, [])
        self.assertEqual(self.policy()["consumed_checks"], 2)

    def test_busy_worker_is_not_steered_and_does_not_consume_a_continuation(self):
        self.prepare()
        self.worker.info["thread"]["status"] = {"type": "active", "activeFlags": []}
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "pending")
        self.assertEqual(self.policy()["consumed_continuations"], 0)
        self.assertEqual(self.worker.starts, [])

    def test_queued_input_stops_delivery(self):
        self.prepare()
        self.worker.queued = True
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
        self.assertEqual(self.worker.starts, [])

    def test_changed_permissions_or_workspace_or_thread_block_delivery(self):
        self.prepare()
        for key, value in (("cwd", str(self.root)), ("sandbox", {"type": "dangerFullAccess"}),
                           ("approvalPolicy", "never"), ("approvalsReviewer", "auto_review")):
            original = self.worker.info[key]
            self.worker.info[key] = value
            with self.subTest(key=key):
                self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
                self.assertEqual(self.worker.starts, [])
            self.worker.info[key] = original
            self.store.update(self.job_id, state="pending")
        self.worker.info["thread"]["id"] = "different-worker"
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")

    def test_unavailable_approval_keeps_the_event_reserved_for_operator_review(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        self.worker.approval = True
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.policy()["consumed_continuations"], 1)

    def test_revoking_pending_delivery_does_not_kill_or_relaunch_a_job(self):
        self.prepare(complete=False)
        self.registry.reserve_launch(self.job_id)
        self.store.revoke(self.grant["grant_id"])
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "revoked")
        result = run_wrapper(self.registry, self.job_id)
        self.assertEqual(result["observed_state"], "succeeded")
        self.assertEqual(self.worker.starts, [])

    def test_revocation_after_reservation_does_not_restore_allowance(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        self.store.revoke(self.grant["grant_id"])
        self.worker.finish()
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "completed")
        self.assertEqual(self.policy()["consumed_continuations"], 1)
        self.assertIsNotNone(self.policy()["revoked_at"])

    def test_late_status_update_cannot_resurrect_a_revoked_event(self):
        self.prepare()
        self.store.revoke(self.grant["grant_id"])
        self.assertEqual(self.store.update(self.job_id, state="pending")["state"], "revoked")

    def test_continuation_allowance_carries_to_successor_jobs(self):
        self.prepare(continuations=1)
        self.dispatcher.step(self.job_id)
        self.worker.finish()
        self.dispatcher.step(self.job_id)
        successor = self.registry.register(self.grant["grant_id"], [sys.executable, "-c", "print(2)"])
        with self.assertRaises(SchedulerError):
            DeliveryStore(Registry(self.registry.root)).arm(successor["job_id"], "Finish successor")
        with self.assertRaises(SchedulerError):
            self.store.configure(self.grant["grant_id"], socket_path=self.root / "other.sock")
        self.assertEqual(self.policy()["consumed_continuations"], 1)

    def test_check_allowance_persists_across_restart(self):
        self.prepare(complete=False, checks=1)
        self.dispatcher.step(self.job_id)
        with self.assertRaises(SchedulerError):
            Dispatcher(Registry(self.registry.root), self.worker).step(self.job_id)
        self.assertEqual(self.policy()["consumed_checks"], 1)

    def test_a_new_job_cannot_reset_an_exhausted_check_allowance(self):
        self.prepare(checks=2)
        self.dispatcher.step(self.job_id)
        self.worker.finish()
        self.dispatcher.step(self.job_id)
        successor = self.registry.register(self.grant["grant_id"], [sys.executable, "-c", "print(2)"])
        with self.assertRaises(SchedulerError):
            self.store.arm(successor["job_id"], "Finish successor")
        self.assertEqual(self.policy()["consumed_checks"], 2)

    def test_watch_exits_after_one_completed_event(self):
        self.prepare()
        start = self.worker.start

        def finish_after_start(event):
            turn = start(event)
            self.worker.finish()
            return turn

        self.worker.start = finish_after_start
        self.assertEqual(self.dispatcher.watch(self.job_id, interval=0.05)["state"], "completed")
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.policy()["consumed_checks"], 2)

    def test_waiting_on_approval_flags_stop_a_running_event(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        self.worker.info["thread"]["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
        self.assertEqual(len(self.worker.starts), 1)

    def test_history_cannot_replace_an_acknowledged_turn_with_another(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        self.worker.history[0]["id"] = "different-turn"
        self.worker.finish()
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")

    def test_another_workspace_cannot_arm_the_same_busy_worker(self):
        self.prepare()
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        grant = self.registry.create_grant(workspace=other_workspace, worker_thread="synthetic-worker", scope="other fixture")
        self.store.configure(grant["grant_id"], socket_path=self.root / "other.sock")
        job = self.registry.register(grant["grant_id"], [sys.executable, "-c", "print(2)"])
        with self.assertRaises(SchedulerError):
            self.store.arm(job["job_id"], "Finish other fixture")

    def test_worker_and_workspace_stay_reserved_until_continuation_finishes(self):
        self.prepare()
        another = self.registry.create_grant(workspace=self.workspace, worker_thread="another-worker", scope="other fixture")
        with self.assertRaises(SchedulerError):
            self.registry.register(another["grant_id"], [sys.executable, "-c", "print(2)"])
        self.dispatcher.step(self.job_id)
        self.worker.finish()
        self.dispatcher.step(self.job_id)
        self.registry.register(another["grant_id"], [sys.executable, "-c", "print(2)"])

    def test_second_controller_cannot_send_while_event_lease_is_held(self):
        self.prepare()
        event = self.store.event(self.job_id)
        with exclusive_lease(self.registry.root / ("delivery-" + event["event_id"] + ".lock")):
            with self.assertRaises(SchedulerError):
                self.dispatcher.step(self.job_id)
        self.assertEqual(self.worker.starts, [])
        self.assertEqual(self.policy()["consumed_checks"], 0)

    def test_second_waiter_cannot_consume_checks_while_a_watcher_owns_the_event(self):
        self.prepare()
        event = self.store.event(self.job_id)
        with exclusive_lease(self.registry.root / ("watcher-" + event["event_id"] + ".lock")):
            with self.assertRaises(SchedulerError):
                self.dispatcher.watch(self.job_id, interval=0.05)
        self.assertEqual(self.worker.starts, [])
        self.assertEqual(self.policy()["consumed_checks"], 0)

    def test_receipt_change_after_dispatch_blocks_reconciliation(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        result = self.registry.inspect(self.job_id)
        receipt = {**result["receipt"], "completed_at": "changed-local-record"}
        atomic_json(self.registry.job_directory(self.job_id) / "completion.json", receipt)
        self.worker.finish()
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")

    def test_duplicate_history_and_failed_turns_never_send_again(self):
        self.prepare()
        self.dispatcher.step(self.job_id)
        self.worker.history.append({"id": "other-turn", "status": "completed"})
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "uncertain")
        self.worker.history.pop()
        self.worker.finish("failed")
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "failed")
        self.assertEqual(len(self.worker.starts), 1)

    def test_reconcile_only_never_dispatches_an_unused_event(self):
        self.prepare()
        self.assertEqual(self.dispatcher.step(self.job_id, reconcile_only=True)["state"], "pending")
        self.assertEqual(self.worker.starts, [])

    def test_upgrade_keeps_old_job_counters_and_does_not_enable_delivery(self):
        grant = self.registry.create_grant(workspace=self.workspace, worker_thread="synthetic-worker", scope="fixture")
        job = self.registry.register(grant["grant_id"], [sys.executable, "-c", "print(1)"])
        self.registry.reserve_launch(job["job_id"])
        with self.registry.connect(write=True) as connection:
            connection.execute("DROP TABLE deliveries")
            connection.execute("DROP TABLE continuation_grants")
            connection.execute("PRAGMA user_version = 1")
        old = Registry(self.registry.root)
        self.assertEqual(old.grant(grant["grant_id"])["consumed_jobs"], 1)
        with self.assertRaises(SchedulerError):
            DeliveryStore(old)
        updated = Registry(self.registry.root, create=True)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.grant(grant["grant_id"])["consumed_jobs"], 1)
        with self.assertRaises(SchedulerError):
            DeliveryStore(updated).policy(grant["grant_id"])

    def test_cli_delivery_status_does_not_connect_or_consume_checks(self):
        self.prepare()
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli(["--state-dir", str(self.registry.root), "delivery-status", "--job", self.job_id])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "pending")
        self.assertEqual(self.policy()["consumed_checks"], 0)


class AdapterTests(unittest.TestCase):
    def adapter_with_pages(self, pages):
        adapter = CodexAdapter({"socket_path": "unused"})
        calls = []

        def call(method, params):
            calls.append((method, params))
            return pages.pop(0)

        adapter.call = call
        return adapter, calls

    def event(self):
        return {"worker_thread": "synthetic-worker", "event_id": "synthetic-event", "request_text": "exact event"}

    def turn(self, turn_id="turn", text="exact event"):
        return {"id": turn_id, "status": "completed", "itemsView": "full", "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": text}]},
        ]}

    def test_history_matches_exact_event_input_across_pages(self):
        adapter, calls = self.adapter_with_pages([
            {"data": [self.turn("other", "unrelated")], "nextCursor": "page2"},
            {"data": [self.turn()], "nextCursor": None},
        ])
        self.assertEqual(adapter.find_event(self.event()), [{"id": "turn", "status": "completed"}])
        self.assertEqual(calls[1][1]["cursor"], "page2")

    def test_history_that_mixes_user_input_is_not_accepted_as_an_owned_turn(self):
        turn = self.turn()
        turn["items"].append({"type": "userMessage", "content": [{"type": "text", "text": "another user"}]})
        adapter, _ = self.adapter_with_pages([{"data": [turn]}])
        with self.assertRaises(NeedsOperator):
            adapter.find_event(self.event())

    def test_truncated_history_stops_at_the_page_budget(self):
        adapter, calls = self.adapter_with_pages([{"data": [], "nextCursor": "more"}] * 5)
        with self.assertRaises(NeedsOperator):
            adapter.find_event(self.event())
        self.assertEqual(len(calls), 5)

    def test_start_uses_exact_thread_event_id_and_no_permission_overrides(self):
        adapter, calls = self.adapter_with_pages([{"turn": {"id": "turn"}}])
        self.assertEqual(adapter.start(self.event()), "turn")
        self.assertEqual(calls, [("turn/start", {
            "threadId": "synthetic-worker", "clientUserMessageId": "synthetic-event",
            "input": [{"type": "text", "text": "exact event"}],
        })])

    def test_rpc_server_requests_stop_without_answering_approvals(self):
        class Connection:
            def __init__(self):
                self.sent = []

            def send(self, value):
                self.sent.append(json.loads(value))

            def recv(self, timeout):
                return json.dumps({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})

        adapter = CodexAdapter({"socket_path": "unused"})
        adapter.connection = Connection()
        with self.assertRaises(NeedsOperator):
            adapter.call("thread/resume", {"threadId": "synthetic-worker"})
        self.assertEqual(len(adapter.connection.sent), 1)
        self.assertEqual(adapter.connection.sent[0]["method"], "thread/resume")


if __name__ == "__main__":
    unittest.main()
