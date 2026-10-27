"""Quota visibility and bounded recovery, using synthetic workers without inference."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.codex import CodexAdapter
from codex_scheduler.delivery import DEFAULT_PERMISSIONS, DeliveryStore, Dispatcher, NeedsOperator
from codex_scheduler.progress import ProgressObserver
from codex_scheduler.runner import run_wrapper
from codex_scheduler.storage import atomic_json
from codex_scheduler.usage import error_code, quota_snapshot, read_quota


def limits(used=100, reset=None, minutes=300):
    return {"rateLimits": {"limitId": "codex", "primary": {
        "usedPercent": used, "windowDurationMins": minutes,
        "resetsAt": int(time.time()) + 60 if reset is None else reset}}}


class QuotaTests(unittest.TestCase):
    def test_reads_percentage_duration_and_reset_without_account_or_credit_details(self):
        response = limits(25, 2000000000)
        response["email"] = "private@example.invalid"
        response["rateLimitResetCredits"] = {"id": "private-reset-credit"}
        result = quota_snapshot(response)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["buckets"][0]["windows"][0], {
            "name": "primary", "used_percent": 25, "window_minutes": 300, "reset_at": 2000000000})
        self.assertNotIn("private", json.dumps(result))

    def test_all_reported_buckets_and_windows_must_be_available(self):
        primary = limits(reset=2000000000)["rateLimits"]
        primary["secondary"] = {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 2000000500}
        result = quota_snapshot({"rateLimits": limits(0)["rateLimits"],
                                 "rateLimitsByLimitId": {"codex": primary, "other": limits(20)["rateLimits"]}})
        self.assertEqual(result["availability"], "exhausted")
        self.assertEqual(result["reset_at"], 2000000500)

    def test_missing_and_malformed_limits_remain_unknown_and_elapsed_reset_is_not_refill(self):
        for response in (None, {}, {"rateLimits": {}}, limits(True), limits(-1), limits(float("nan")),
                         {"rateLimitsByLimitId": {str(i): {} for i in range(33)}}):
            with self.subTest(response=response):
                self.assertEqual(quota_snapshot(response)["availability"], "unknown")
        self.assertEqual(quota_snapshot(limits(100, 1))["availability"], "exhausted")
        self.assertEqual(quota_snapshot(limits(20, minutes=None))["buckets"][0]["windows"][0]["window_minutes"], None)

    def test_backend_spend_and_unexplained_limit_flags_do_not_authorize_resumes(self):
        for fields in ({"spendControlReached": True}, {"individualLimit": {"remainingPercent": 0}},
                       {"rateLimitReachedType": "workspace_owner_credits_depleted"}):
            response = limits(20)
            response["rateLimits"].update(fields)
            result = quota_snapshot(response)
            self.assertEqual(result["availability"], "exhausted")
            self.assertIsNone(result["reset_at"])
        response = limits(100)
        response["rateLimits"]["rateLimitReachedType"] = "workspace_owner_credits_depleted"
        self.assertIsNone(quota_snapshot(response)["reset_at"])

    def test_sparse_notification_keeps_other_buckets_blocked(self):
        previous = quota_snapshot({"rateLimitsByLimitId": {
            "codex": limits(0)["rateLimits"], "other": {**limits(100)["rateLimits"], "limitId": "other"}}})
        updated = quota_snapshot(limits(10), previous=previous)
        self.assertEqual(updated["availability"], "exhausted")
        self.assertEqual(len(updated["buckets"]), 2)
        unknown = quota_snapshot(limits(10), previous=quota_snapshot())
        self.assertEqual(unknown["availability"], "unknown")

    def test_error_codes_follow_the_installed_wire_schema_and_operator_requests_propagate(self):
        for value in ("usageLimitExceeded", "UsageLimitExceeded", {"usageLimitExceeded": {}}):
            self.assertEqual(error_code({"codexErrorInfo": value}), "UsageLimitExceeded")
        self.assertNotEqual(error_code({"codexErrorInfo": "rateLimitExceeded"}), "UsageLimitExceeded")
        self.assertIsNone(error_code({"message": "usage limit exceeded"}))
        self.assertEqual(read_quota(Mock(rate_limits=Mock(side_effect=SchedulerError("unsupported"))))["availability"], "unknown")
        with self.assertRaises(NeedsOperator):
            read_quota(Mock(rate_limits=Mock(side_effect=NeedsOperator("operator"))))

    def test_sparse_primary_notification_does_not_erase_weekly_or_spend_blocks(self):
        response = limits(10)
        response["rateLimits"]["secondary"] = {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 2000000000}
        previous = quota_snapshot(response)
        updated = quota_snapshot(limits(0), previous=previous)
        self.assertEqual(updated["availability"], "exhausted")
        self.assertEqual(updated["reset_at"], 2000000000)
        previous = quota_snapshot({"rateLimits": {**limits(0)["rateLimits"], "spendControlReached": True}})
        self.assertEqual(quota_snapshot(limits(0), previous=previous)["availability"], "exhausted")


class Worker:
    def __init__(self, workspace):
        self.info = {**DEFAULT_PERMISSIONS, "cwd": str(workspace),
                     "thread": {"id": "synthetic-worker", "status": {"type": "idle"}}}
        self.quota = limits()
        self.history = []
        self.starts = []
        self.queued = False
        self.error_after_send = False
        self.on_send = None
        self.on_quota = None

    def __call__(self, spec):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def snapshot(self, thread):
        return self.info

    def has_queued_input(self, thread):
        return self.queued

    def start(self, event):
        if self.on_send:
            self.on_send(event)
        self.starts.append(dict(event))
        turn = {"id": "turn-" + str(len(self.starts)), "status": "inProgress", "request_text": event["request_text"]}
        self.history.append(turn)
        self.info["thread"]["status"] = {"type": "active"}
        if self.error_after_send:
            raise TimeoutError("synthetic lost acknowledgement")
        return turn["id"]

    def find_event(self, event):
        return [dict(turn) for turn in self.history if turn["request_text"] == event["request_text"]]

    def latest_turn(self, thread):
        return dict(self.history[-1]) if self.history else None

    def rate_limits(self):
        if self.on_quota:
            self.on_quota()
        return self.quota

    def finish(self, code=None):
        self.history[-1]["status"] = "failed" if code else "completed"
        if code:
            self.history[-1]["error_code"] = error_code({"codexErrorInfo": code})
        self.info["thread"]["status"] = {"type": "idle"}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.registry = Registry(self.root / "state", create=True)
        self.grant_id = self.registry.create_grant(workspace=self.workspace, worker_thread="synthetic-worker",
                                                  scope="Finish one synthetic engineering slice", max_jobs=2)["grant_id"]
        self.store = DeliveryStore(self.registry)
        self.worker = Worker(self.workspace)
        self.dispatcher = Dispatcher(self.registry, self.worker)

    def prepare(self, resumes=1, total=3, checks=30):
        self.store.configure(self.grant_id, socket_path=self.root / "worker.sock", max_usage_resumes=resumes,
                             max_continuations=total, max_checks=checks)
        self.job_id = self.registry.register(self.grant_id, [sys.executable, "-c", "print(45)"])["job_id"]
        self.store.arm(self.job_id, "Read the receipt, report the result, and stop.")
        self.registry.reserve_launch(self.job_id)
        run_wrapper(self.registry, self.job_id)

    def pause(self):
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "started")
        self.worker.finish("usageLimitExceeded")
        event = self.dispatcher.step(self.job_id)
        self.assertEqual(event["usage"]["state"], "blocked_on_usage")
        return event

    def test_recovery_reserves_before_send_and_finishes_same_scope_without_relaunch(self):
        self.prepare()
        original = self.pause()
        self.worker.quota = limits(5)

        def check_reserved(event):
            saved = self.store.event(self.job_id)
            policy = self.store.policy(self.grant_id)
            self.assertEqual(saved["usage"]["resume"]["state"], "dispatching")
            self.assertEqual(saved["usage"]["resume"]["request_text"], event["request_text"])
            self.assertEqual(policy["consumed_usage_resumes"], 1)
            self.assertEqual(policy["consumed_continuations"], 2)

        self.worker.on_send = check_reserved
        resumed = self.dispatcher.step(self.job_id)
        self.assertEqual(resumed["state"], "started")
        payload = json.loads(self.worker.starts[-1]["request_text"])
        self.assertEqual(payload["scope"], json.loads(original["request_text"])["scope"])
        self.assertEqual(payload["interrupted_turn"], "turn-1")
        self.assertEqual(payload["original_event_id"], original["event_id"])
        self.worker.finish()
        final = self.dispatcher.step(self.job_id)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["turn_id"], "turn-1")
        self.assertEqual(final["usage"]["resume"]["turn_id"], "turn-2")
        self.assertEqual(self.registry.grant(self.grant_id)["consumed_jobs"], 1)
        self.assertEqual(len(self.worker.starts), 2)

    def test_recovery_is_opt_in_and_also_requires_total_continuation_allowance(self):
        for resumes, total in ((0, 3), (1, 1)):
            with self.subTest(resumes=resumes, total=total):
                self.setUp()
                self.prepare(resumes=resumes, total=total)
                self.pause()
                before = self.store.policy(self.grant_id)
                self.worker.quota = limits(0)
                self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
                self.assertEqual(self.store.policy(self.grant_id), before)
                self.assertEqual(len(self.worker.starts), 1)

    def test_repeated_exhaustion_preserves_attempts_and_cannot_refill_policy(self):
        self.prepare(resumes=1, total=4)
        self.pause()
        self.worker.quota = limits(0)
        self.dispatcher.step(self.job_id)
        self.worker.finish("usageLimitExceeded")
        second = self.dispatcher.step(self.job_id)
        self.assertEqual(second["usage"]["source_turn_id"], "turn-2")
        restarted = Dispatcher(Registry(self.registry.root, create=True), self.worker)
        self.assertEqual(restarted.step(self.job_id)["state"], "blocked")
        self.assertEqual(len(self.worker.starts), 2)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 1)
        with self.assertRaises(SchedulerError):
            self.store.configure(self.grant_id, socket_path=self.root / "new.sock", max_usage_resumes=10)

    def test_lost_recovery_acknowledgement_is_reconciled_without_replay(self):
        self.prepare()
        self.pause()
        self.worker.quota = limits(0)
        self.worker.error_after_send = True
        unknown = self.dispatcher.step(self.job_id)
        self.assertEqual(unknown["state"], "uncertain")
        self.assertEqual(unknown["usage"]["resume"]["state"], "uncertain")
        self.worker.finish()
        restarted = Dispatcher(Registry(self.registry.root), self.worker)
        self.assertEqual(restarted.step(self.job_id, reconcile_only=True)["state"], "completed")
        self.assertEqual(len(self.worker.starts), 2)

    def test_crash_after_recovery_reservation_with_no_history_never_authorizes_another_send(self):
        self.prepare()
        self.pause()
        self.store.pause_for_usage(self.job_id, "turn-1", quota_snapshot(limits(0)))
        self.store.reserve_usage_resume(self.job_id)
        restarted = Dispatcher(Registry(self.registry.root), self.worker)
        for _ in range(2):
            self.assertEqual(restarted.step(self.job_id, reconcile_only=True)["state"], "uncertain")
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 2)

    def test_successor_job_and_restart_do_not_reset_usage_recovery_allowance(self):
        self.prepare(resumes=1, total=4)
        self.pause()
        self.worker.quota = limits(0)
        self.dispatcher.step(self.job_id)
        self.worker.finish()
        self.dispatcher.step(self.job_id)
        successor = self.registry.register(self.grant_id, [sys.executable, "-c", "print(46)"])["job_id"]
        self.store.arm(successor, "Report and stop")
        self.registry.reserve_launch(successor)
        run_wrapper(self.registry, successor)
        self.dispatcher.step(successor)
        self.worker.finish("usageLimitExceeded")
        self.dispatcher.step(successor)
        restarted = Dispatcher(Registry(self.registry.root), self.worker)
        self.assertEqual(restarted.step(successor)["state"], "blocked")
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 1)
        self.assertEqual(len(self.worker.starts), 3)

    def test_revoke_before_and_after_reservation_keeps_consumed_allowance(self):
        self.prepare()
        self.pause()
        self.worker.quota = limits(0)
        reserve = self.store.reserve_usage_resume

        def revoke_after_reservation(job):
            result = reserve(job)
            self.store.revoke(self.grant_id)
            return result

        with patch.object(self.dispatcher.store, "reserve_usage_resume", side_effect=revoke_after_reservation):
            self.assertEqual(self.dispatcher.step(self.job_id)["state"], "revoked")
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_continuations"], 2)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 1)

    def test_revoke_while_waiting_sends_nothing(self):
        self.prepare()
        self.pause()
        self.store.revoke(self.grant_id)
        self.assertEqual(self.store.event(self.job_id)["state"], "revoked")
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "revoked")
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 0)

    def test_permission_queue_latest_turn_and_approval_changes_stop_recovery(self):
        for kind in ("permissions", "workspace", "identity", "queue", "latest", "approval"):
            with self.subTest(kind=kind):
                self.setUp()
                self.prepare()
                self.pause()
                self.worker.quota = limits(0)
                if kind == "permissions":
                    self.worker.info["approvalPolicy"] = "never"
                elif kind == "workspace":
                    self.worker.info["cwd"] = str(self.root)
                elif kind == "identity":
                    self.worker.info["thread"]["id"] = "other-worker"
                elif kind == "queue":
                    self.worker.queued = True
                elif kind == "latest":
                    self.worker.history.append({"id": "other-turn", "status": "completed", "request_text": "unrelated"})
                else:
                    self.worker.info["thread"]["status"]["activeFlags"] = ["waitingOnApproval"]
                result = self.dispatcher.step(self.job_id)
                self.assertEqual(result["state"], "blocked")
                self.assertNotEqual(result["reason"], "blocked_on_usage")
                self.assertEqual(len(self.worker.starts), 1)

    def test_changed_receipt_during_quota_read_cannot_reach_recovery_send(self):
        self.prepare()
        self.pause()
        self.worker.quota = limits(0)

        def change_receipt():
            receipt = self.registry.inspect(self.job_id)["receipt"]
            receipt["completed_at"] = "changed-evidence"
            atomic_json(self.registry.job_directory(self.job_id) / "completion.json", receipt)

        self.worker.on_quota = change_receipt
        self.dispatcher.step(self.job_id)
        self.assertEqual(len(self.worker.starts), 1)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_usage_resumes"], 0)

    def test_reconcile_only_and_unknown_quota_never_start_a_recovery(self):
        self.prepare()
        self.pause()
        self.worker.quota = limits(0)
        self.assertEqual(self.dispatcher.step(self.job_id, reconcile_only=True)["state"], "blocked")
        self.worker.rate_limits = Mock(side_effect=SchedulerError("unsupported"))
        result = self.dispatcher.step(self.job_id)
        self.assertEqual(result["usage"]["quota"]["availability"], "unknown")
        self.assertEqual(len(self.worker.starts), 1)

    def test_exhausted_check_allowance_stops_before_quota_query_or_resume(self):
        self.prepare(checks=2)
        self.pause()
        self.worker.rate_limits = Mock()
        with self.assertRaises(SchedulerError):
            self.dispatcher.step(self.job_id)
        self.worker.rate_limits.assert_not_called()
        self.assertEqual(self.store.policy(self.grant_id)["consumed_checks"], 2)

    def test_waiter_uses_reset_to_schedule_a_fresh_check_without_spending_checks_while_asleep(self):
        self.prepare()
        self.pause()
        start = time.time()
        reset = int(start) + 5
        self.worker.quota = limits(100, reset)
        clock = [start]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            if clock[0] >= reset:
                self.worker.quota = limits(0, reset + 18000)

        find = self.worker.find_event

        def history(event):
            if len(self.worker.starts) == 2:
                self.worker.finish()
            return find(event)

        self.worker.find_event = history
        with patch("codex_scheduler.delivery.time.time", side_effect=lambda: clock[0]), \
                patch("codex_scheduler.delivery.time.sleep", side_effect=sleep):
            result = self.dispatcher.watch(self.job_id, interval=0.05)
        self.assertEqual(result["state"], "completed")
        self.assertGreater(len(sleeps), 1)
        self.assertEqual(self.store.policy(self.grant_id)["consumed_checks"], 5)
        self.assertEqual(len(self.worker.starts), 2)

    def test_waiter_hands_off_when_reset_is_missing_past_or_beyond_hang_guard(self):
        for reset in (None, 1, int(time.time()) + 90000):
            with self.subTest(reset=reset):
                self.setUp()
                self.prepare()
                self.pause()
                self.worker.quota = limits(100, reset or 1)
                self.worker.quota["rateLimits"]["primary"]["resetsAt"] = reset
                with patch("codex_scheduler.delivery.time.sleep") as sleep:
                    self.assertEqual(self.dispatcher.watch(self.job_id)["state"], "blocked")
                sleep.assert_not_called()

    def test_revocation_during_deterministic_wait_does_not_spend_another_check(self):
        self.prepare()
        self.pause()
        with patch("codex_scheduler.delivery.time.sleep", side_effect=lambda seconds: self.store.revoke(self.grant_id)):
            result = self.dispatcher.watch(self.job_id)
        self.assertEqual(result["state"], "revoked")
        self.assertEqual(self.store.policy(self.grant_id)["consumed_checks"], 3)
        self.assertEqual(len(self.worker.starts), 1)

    def test_other_failures_are_not_retried_and_usage_wait_keeps_workspace_reserved(self):
        self.prepare()
        self.pause()
        another = self.registry.create_grant(workspace=self.workspace, worker_thread="other", scope="other")
        with self.assertRaises(SchedulerError):
            self.registry.register(another["grant_id"], [sys.executable, "-c", "print(1)"])
        self.worker.history[0]["error_code"] = "internalServerError"
        self.worker.quota = limits(0)
        self.assertEqual(self.dispatcher.step(self.job_id)["state"], "blocked")
        self.assertEqual(len(self.worker.starts), 1)

    def test_v2_upgrade_preserves_counters_and_does_not_enable_usage_recovery(self):
        self.prepare(resumes=0)
        self.dispatcher.step(self.job_id)
        legacy_spec = self.store.policy(self.grant_id)["spec"]
        del legacy_spec["max_usage_resumes"]
        with self.registry.connect(write=True) as connection:
            connection.execute("UPDATE continuation_grants SET spec = ? WHERE grant_id = ?", (json.dumps(legacy_spec), self.grant_id))
            connection.execute("DROP TABLE usage_waits")
            connection.execute("DROP TABLE usage_resumes")
            connection.execute("PRAGMA user_version = 2")
        old = Registry(self.registry.root)
        self.assertEqual(old.version, 2)
        self.assertEqual(DeliveryStore(old).policy(self.grant_id)["consumed_usage_resumes"], 0)
        upgraded = Registry(self.registry.root, create=True)
        policy = DeliveryStore(upgraded).policy(self.grant_id)
        self.assertEqual(upgraded.version, 3)
        self.assertEqual(policy["consumed_continuations"], 1)
        self.assertEqual(policy["spec"].get("max_usage_resumes", 0), 0)
        self.assertEqual(upgraded.grant(self.grant_id)["consumed_jobs"], 1)


class ProgressUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = Registry(self.root / "state", create=True)
        self.grant_id = self.registry.create_grant(workspace=self.root, worker_thread="synthetic-worker", scope="capture usage fixture")["grant_id"]
        DeliveryStore(self.registry).configure(self.grant_id, socket_path=self.root / "worker.sock")
        self.worker = Worker(self.root)

    def capture(self, events=(), latest=None, job_id=None):
        events = iter(events)

        def read(**kwargs):
            try:
                self.worker.on_notification(next(events))
            except StopIteration:
                raise ConnectionError("fixture closed")
            return True

        self.worker.read_notification = read
        if latest is not None:
            self.worker.latest_turn = lambda thread: latest
        observer = ProgressObserver(self.registry, self.grant_id, adapter_factory=self.worker, job_id=job_id)
        return observer, observer.run()

    def event(self, method, **params):
        return {"method": method, "params": {"threadId": "synthetic-worker", **params}}

    def failed(self):
        return self.event("turn/completed", turn={"id": "turn-a", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}})

    def test_usage_interruption_survives_idle_and_observer_restart_then_clears_on_real_resume(self):
        observer, result = self.capture([self.failed(), self.event("thread/status/changed", status={"type": "idle"})])
        self.assertEqual(result["worker_state"], "idle")
        self.assertEqual(result["task_state"], "blocked_on_usage")
        self.assertEqual(result["usage_interruption"]["turn_id"], "turn-a")
        self.assertIn("blocked_on_usage", Path(observer.locations()["markdown_path"]).read_text())
        _, restarted = self.capture()
        self.assertEqual(restarted["task_state"], "blocked_on_usage")
        _, resumed = self.capture([self.event("turn/started", turn={"id": "turn-b", "status": "inProgress"})])
        self.assertEqual(resumed["task_state"], "worker_turn_active")
        self.assertIsNone(resumed["usage_interruption"])

    def test_initial_latest_turn_probe_detects_usage_failure_while_idle_without_raw_history(self):
        _, result = self.capture(latest={"id": "old-turn", "status": "failed", "error_code": "UsageLimitExceeded"})
        self.assertEqual(result["task_state"], "blocked_on_usage")
        self.assertEqual(result["usage_interruption"]["turn_id"], "old-turn")
        self.assertEqual(self.worker.starts, [])

    def test_quota_refill_alone_never_clears_interruption_or_starts_work(self):
        _, result = self.capture([self.failed(), {"method": "account/rateLimits/updated", "params": limits(0)}])
        self.assertEqual(result["quota"]["availability"], "available")
        self.assertEqual(result["task_state"], "blocked_on_usage")
        self.assertEqual(self.worker.starts, [])

    def test_retryable_error_does_not_get_misclassified_as_usage_interruption(self):
        for retry, expected in ((True, "worker_turn_failed"), (False, "blocked_on_usage")):
            with self.subTest(retry=retry):
                _, result = self.capture([
                    self.event("turn/started", turn={"id": "turn-a", "status": "inProgress"}),
                    self.event("error", turnId="turn-a", willRetry=retry, error={"codexErrorInfo": "usageLimitExceeded", "message": "private backend text"}),
                    self.event("turn/completed", turn={"id": "turn-a", "status": "failed"})])
                self.assertEqual(result["task_state"], expected)
                self.assertNotIn("private backend text", json.dumps(result))

    def test_foreign_turn_error_cannot_block_this_worker(self):
        wrong = self.failed()
        wrong["params"]["threadId"] = "other-worker"
        _, result = self.capture([wrong])
        self.assertNotEqual(result["task_state"], "blocked_on_usage")

    def test_duplicate_start_and_missing_error_detail_do_not_erase_known_interruption(self):
        self.capture([self.failed()])
        _, duplicate = self.capture([self.event("turn/started", turn={"id": "turn-a", "status": "inProgress"})])
        self.assertEqual(duplicate["task_state"], "blocked_on_usage")
        _, restarted = self.capture(latest={"id": "turn-a", "status": "failed", "error_code": None})
        self.assertEqual(restarted["task_state"], "blocked_on_usage")

    def test_usage_status_cli_reads_account_data_without_starting_or_consuming_checks(self):
        output = io.StringIO()
        with patch("codex_scheduler.codex.CodexAdapter", return_value=self.worker), redirect_stdout(output):
            code = cli(["--state-dir", str(self.registry.root), "usage-status", "--grant", self.grant_id])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["quota"]["availability"], "exhausted")
        self.assertEqual(DeliveryStore(self.registry).policy(self.grant_id)["consumed_checks"], 0)
        self.assertEqual(self.worker.starts, [])

    def test_old_delivery_pause_does_not_overwrite_new_worker_activity_or_newer_quota(self):
        job_id = self.registry.register(self.grant_id, [sys.executable, "-c", "print(45)"])["job_id"]
        DeliveryStore(self.registry).arm(job_id, "Report and stop")
        self.registry.reserve_launch(job_id)
        run_wrapper(self.registry, job_id)
        dispatcher = Dispatcher(self.registry, self.worker)
        dispatcher.step(job_id)
        self.worker.finish("usageLimitExceeded")
        dispatcher.step(job_id)
        _, result = self.capture([
            self.event("turn/started", turn={"id": "manual-resume", "status": "inProgress"}),
            {"method": "account/rateLimits/updated", "params": limits(0)}], job_id=job_id)
        self.assertEqual(result["delivery_state"], "blocked")
        self.assertEqual(result["task_state"], "worker_turn_active")
        self.assertEqual(result["quota"]["availability"], "available")


class UsageAdapterTests(unittest.TestCase):
    def test_adapter_uses_bounded_exact_thread_history_and_normalizes_wire_error(self):
        adapter = CodexAdapter({})
        adapter.call = Mock(return_value={"data": [{"id": "turn-a", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}, "items": []}]})
        result = adapter.latest_turn("synthetic-worker")
        self.assertEqual(result, {"id": "turn-a", "status": "failed", "error_code": "UsageLimitExceeded"})
        adapter.call.assert_called_once_with("thread/turns/list", {"threadId": "synthetic-worker", "limit": 1, "itemsView": "notLoaded", "sortDirection": "desc"})
        adapter.call.reset_mock()
        adapter.rate_limits()
        adapter.call.assert_called_once_with("account/rateLimits/read", {})

    def test_failed_matching_delivery_retains_structured_usage_error_for_reconciliation(self):
        adapter = CodexAdapter({})
        adapter.call = Mock(side_effect=[{"data": [{"id": "turn-a", "status": "failed", "items": [],
            "error": {"codexErrorInfo": "usageLimitExceeded", "message": "private backend text"}}]},
            {"data": [{"turnId": "turn-a", "item": {"id": "user-a", "type": "userMessage", "content": [
                {"type": "text", "text": "exact saved request"}]}}]}])
        result = adapter.find_event({"worker_thread": "synthetic-worker", "turn_id": "turn-a", "request_text": "exact saved request"})
        self.assertEqual(result, [{"id": "turn-a", "status": "failed", "error_code": "UsageLimitExceeded"}])


if __name__ == "__main__":
    unittest.main()
