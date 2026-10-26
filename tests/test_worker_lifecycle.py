"""Offline checks for the trial's limits and observable lifecycle behavior."""

import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest

from prototypes.worker_lifecycle import Harness, Rpc, Trial, TrialError, verify_completion


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "trial"
        self.trial = Trial(self.path, "queue")

    def saved(self):
        return json.loads((self.path / "status.json").read_text())

    def test_existing_run_cannot_reset_allowance(self):
        self.trial.write(thread_id="synthetic-worker")
        self.trial.reserve("job")
        with self.assertRaises(FileExistsError):
            Trial(self.path, "queue")
        self.assertEqual(self.saved()["job_attempts"], 1)

    def test_bootstrap_retry_preserves_the_grant_and_history(self):
        original_job = self.trial.state["job_id"]
        self.trial.write(phase="stopped", error="Transport not initialized")
        retried = Trial(self.path, "queue", retry_bootstrap=True)
        self.assertEqual(retried.state["job_id"], original_job)
        self.assertEqual(retried.state["max_continuations"], 1)
        self.assertEqual(retried.state["continuation_attempts"], 0)
        self.assertEqual(retried.state["bootstrap_history"][0]["phase"], "stopped")

    def test_bootstrap_retry_rejects_a_known_worker_or_consumed_allowance(self):
        self.trial.write(phase="stopped", thread_id="synthetic-worker")
        with self.assertRaises(TrialError):
            Trial(self.path, "queue", retry_bootstrap=True)
        self.trial.write(thread_id=None, continuation_attempts=1)
        with self.assertRaises(TrialError):
            Trial(self.path, "queue", retry_bootstrap=True)

    def test_unknown_worker_creation_is_not_a_retryable_bootstrap(self):
        self.trial.write(phase="stopped", stopped_phase="creating_worker")
        with self.assertRaises(TrialError):
            Trial(self.path, "queue", retry_bootstrap=True)

    def test_job_requires_an_exact_worker_and_can_only_launch_once(self):
        with self.assertRaises(TrialError):
            self.trial.reserve("job")
        self.assertEqual(self.saved()["job_attempts"], 0)
        self.trial.write(thread_id="synthetic-worker")
        self.trial.reserve("job")
        with self.assertRaises(TrialError):
            self.trial.reserve("job")
        self.assertEqual(self.saved()["job_attempts"], 1)

    def test_unverified_completion_cannot_consume_a_continuation(self):
        self.trial.write(thread_id="synthetic-worker", phase="job_running")
        with self.assertRaises(TrialError):
            self.trial.reserve("continuation")
        self.assertEqual(self.saved()["continuation_attempts"], 0)

    def test_continuation_is_consumed_before_an_uncertain_effect(self):
        self.trial.write(thread_id="synthetic-worker", phase="job_completed")
        self.trial.reserve("continuation")
        self.assertEqual(self.saved()["continuation_attempts"], 1)
        self.trial.write(phase="job_completed")
        with self.assertRaises(TrialError):
            self.trial.reserve("continuation")

    def test_unattended_approval_never_receives_an_acceptance(self):
        harness = Harness(self.trial, "codex", 5)
        replies = []

        class Connection:
            send = staticmethod(replies.append)

        with self.assertRaises(TrialError):
            harness.handle_request(Connection(), {
                "id": 7, "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "synthetic-worker"},
            })
        self.assertEqual(self.saved()["phase"], "needs_operator")
        self.assertIn("error", replies[0])
        self.assertNotIn("result", replies[0])
        self.assertIsNone(harness.job)

    def test_wrong_worker_cannot_launch_the_job(self):
        self.trial.write(thread_id="expected-worker")
        harness = Harness(self.trial, "codex", 5)
        harness.initial_turn = "initial-turn"

        class Connection:
            send = staticmethod(lambda message: None)

        with self.assertRaises(TrialError):
            harness.handle_request(Connection(), {
                "id": 1, "method": "item/tool/call", "params": {
                    "tool": "start_synthetic_job", "threadId": "other-worker",
                    "turnId": "initial-turn", "arguments": {},
                },
            })
        self.assertEqual(self.saved()["job_attempts"], 0)

    def test_real_job_survives_tool_response_and_waits_for_release(self):
        self.trial.write(thread_id="synthetic-worker")
        (self.path / "workspace").mkdir()
        harness = Harness(self.trial, "codex", 5)
        harness.initial_turn = "initial-turn"
        replies = []

        class Connection:
            send = staticmethod(replies.append)

        try:
            harness.handle_request(Connection(), {
                "id": 1, "method": "item/tool/call", "params": {
                    "tool": "start_synthetic_job", "threadId": "synthetic-worker",
                    "turnId": "initial-turn", "arguments": {},
                },
            })
            self.assertIsNone(harness.job.poll())
            self.assertTrue(replies[0]["result"]["success"])
            self.assertEqual(self.saved()["job_attempts"], 1)
            (self.path / "release").touch()
            self.assertEqual(harness.job.wait(timeout=3), 0)
            harness.job_log.close()
            self.assertEqual(json.loads((self.path / "job.log").read_text()),
                             {"result": 45, "status": "completed"})
        finally:
            if harness.job and harness.job.poll() is None:
                harness.job.kill()
                harness.job.wait(timeout=3)
            if harness.job_log and not harness.job_log.closed:
                harness.job_log.close()


class CompletionTests(unittest.TestCase):
    def test_retained_context_is_required(self):
        entries = [{"item": {"type": "agentMessage", "text": "CONTINUED:wrong:job:0"}}]
        with self.assertRaises(TrialError):
            verify_completion(entries, "CONTINUED:remembered:job:0")

    def test_matching_text_does_not_hide_an_extra_tool_call(self):
        expected = "CONTINUED:remembered:job:0"
        entries = [{"item": {"type": "agentMessage", "text": expected}},
                   {"item": {"type": "dynamicToolCall"}}]
        with self.assertRaises(TrialError):
            verify_completion(entries, expected)

    def test_a_quiet_completion_matches(self):
        expected = "CONTINUED:remembered:job:0"
        verify_completion([{"item": {"type": "agentMessage", "text": expected}}], expected)


class RpcTests(unittest.TestCase):
    def test_websocket_messages_are_sent_as_individual_json_frames(self):
        class Connection:
            def __init__(self):
                self.incoming = queue.Queue()
                self.sent = []

            def __iter__(self):
                while True:
                    message = self.incoming.get()
                    if message is None:
                        return
                    yield message

            def send(self, message):
                self.sent.append(message)
                request = json.loads(message)
                self.incoming.put(json.dumps({"id": request["id"], "result": {"ok": True}}))

            def close(self):
                self.incoming.put(None)

        connection = Connection()
        rpc = Rpc(None, None, lambda *_: None, 3, connection=connection)
        try:
            self.assertEqual(rpc.call("trial/check", {}), {"ok": True})
            self.assertEqual(len(connection.sent), 1)
            self.assertEqual(json.loads(connection.sent[0])["method"], "trial/check")
        finally:
            rpc.close()

    def test_events_and_server_requests_can_arrive_before_a_response(self):
        script = '''import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"method":"turn/completed", "params":{
    "threadId":"other-worker", "turn":{"id":"other", "status":"completed"}}}), flush=True)
print(json.dumps({"id":99, "method":"trial/request", "params":{}}), flush=True)
answer = json.loads(sys.stdin.readline())
assert answer == {"id":99, "result":{}}
print(json.dumps({"id":request["id"], "result":{"ok":True}}), flush=True)
print(json.dumps({"method":"turn/completed", "params":{
    "threadId":"worker", "turn":{"id":"turn", "status":"completed"}}}), flush=True)
sys.stdin.read()
'''
        with tempfile.TemporaryFile(mode="w+") as log:
            rpc = Rpc([sys.executable, "-c", script], log,
                      lambda client, message: client.send({"id": message["id"], "result": {}}), 3)
            try:
                self.assertEqual(rpc.call("trial/check", {}), {"ok": True})
                self.assertEqual(rpc.completed_turn("worker", "turn")["id"], "turn")
                self.assertEqual(len(rpc.history), 2)
            finally:
                rpc.close()


if __name__ == "__main__":
    unittest.main()
