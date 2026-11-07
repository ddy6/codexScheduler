"""Deadline, review and recovery invariants for the standalone skill script."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/poll-review/scripts/timer.py"
SPEC = importlib.util.spec_from_file_location("poll_timer", SCRIPT)
timer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(timer)
BINDING = dict(task="example-check", generation="1", cycle="1", root="example-root",
               helper="example-helper", job_kind="exec-session", job_handle="example-session")


class FakeClock:
    def __init__(self):
        self.elapsed = 0
        self.wall_drift = 0
        self.boot = "example-boot"

    def sample(self):
        return {"mono_ns": int((100 + self.elapsed) * 1e9),
                "wall_ns": int((2000 + self.elapsed + self.wall_drift) * 1e9),
                "boot": self.boot}

    def advance(self, seconds):
        self.elapsed += seconds


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.record = self.root / "timer"
        self.clock = FakeClock()
        self.addCleanup(patch.stopall)
        patch.object(timer, "clock_sample", self.clock.sample).start()
        self.snapshot = self.root / "change.source"
        self.snapshot.write_text("example source\n")
        self.digest = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()

    def start(self, seconds=30, margin=10):
        return timer.start(self.record, BINDING, seconds, margin)

    def test_deadline_preserves_exact_binding_and_never_moves_on_status(self):
        record = self.start()
        self.assertEqual(record["binding"], BINDING)
        original = (self.record / "timer.json").read_bytes()
        self.clock.advance(10)
        self.assertEqual(timer.status(self.record)["remaining_seconds"], 20)
        self.clock.advance(22)
        due = timer.status(self.record)
        self.assertTrue(due["due"])
        self.assertEqual(due["late_seconds"], 2)
        self.assertFalse(due["review_allowed"])
        self.assertEqual((self.record / "timer.json").read_bytes(), original)

    def test_duplicate_start_and_existing_paths_preserve_records(self):
        self.start()
        original = timer.load(self.record)
        self.clock.advance(100)
        with self.assertRaises(FileExistsError):
            self.start(200)
        self.assertEqual(timer.load(self.record), original)
        other = self.root / "link"
        other.symlink_to(self.root / "absent")
        with self.assertRaises(FileExistsError):
            timer.start(other, BINDING, 1)
        self.assertFalse((self.root / "absent").exists())

    def test_invalid_intervals_and_bindings_create_no_records(self):
        for value in (0, -1, float("nan"), float("inf"), timer.MAX_SECONDS + 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.start(value)
            self.assertFalse(self.record.exists())
        for binding in ({}, {**BINDING, "root": ""}, {**BINDING, "cycle": "x\ny"},
                        {**BINDING, "helper": "x" * 257}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                timer.start(self.record, binding, 10)
            self.assertFalse(self.record.exists())

    def test_missing_malformed_and_oversized_records_never_become_due(self):
        with self.assertRaises(FileNotFoundError):
            timer.status(self.record)
        self.assertFalse(self.record.exists())
        self.record.mkdir()
        for raw in (b"{", b"{}", b"x" * (timer.MAX_RECORD_BYTES + 1)):
            (self.record / "timer.json").write_bytes(raw)
            with self.assertRaises(ValueError):
                timer.status(self.record)

    def test_boot_clock_regression_and_wall_discontinuity_require_reconciliation(self):
        self.start()
        for change in ({"boot": "another-boot"}, {"elapsed": -1}, {"wall_drift": 3}):
            with self.subTest(change=change), patch.multiple(self.clock, **change):
                with self.assertRaisesRegex(ValueError, "continuity"):
                    timer.status(self.record)
                with self.assertRaises(ValueError):
                    timer.wait(self.record)
                self.assertFalse((self.record / "wait.claim").exists())

    def test_wait_uses_remaining_time_and_chunks_without_model_or_job_operations(self):
        self.start(180)
        self.clock.advance(50)
        with patch.object(timer.time, "sleep", side_effect=self.clock.advance) as sleep:
            result = timer.wait(self.record)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 60, 10])
        self.assertTrue(result["due"])
        self.assertEqual(result["binding"], BINDING)
        self.assertEqual(json.loads((self.record / "outcome.json").read_text()), result)
        with self.assertRaises(FileExistsError):
            timer.wait(self.record)

    def test_concurrent_waiter_is_refused_and_cancellation_stops_owner(self):
        self.start()
        entered = threading.Event()
        release = threading.Event()
        results = []

        def sleep(_):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test failed to release waiter")

        with patch.object(timer.time, "sleep", side_effect=sleep):
            thread = threading.Thread(target=lambda: results.append(timer.wait(self.record)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(FileExistsError):
                    timer.wait(self.record)
                timer.cancel(self.record)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["state"], "cancelled")

    def test_cancel_is_idempotent_and_suppresses_an_already_due_deadline(self):
        self.start()
        self.clock.advance(31)
        self.assertTrue(timer.status(self.record)["due"])
        timer.cancel(self.record)
        timer.cancel(self.record)
        with patch.object(timer.time, "sleep") as sleep:
            self.assertEqual(timer.wait(self.record)["state"], "cancelled")
            sleep.assert_not_called()
        self.assertFalse(timer.status(self.record)["due"])

    def test_ctrl_c_cancels_and_crashed_wait_cannot_be_replayed(self):
        self.start()
        with patch.object(timer.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(timer.wait(self.record)["state"], "cancelled")
        self.record = self.root / "crash"
        self.start()
        with patch.object(timer.time, "sleep", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                timer.wait(self.record)
        self.assertTrue(timer.status(self.record)["wait_claimed"])
        self.assertFalse((self.record / "outcome.json").exists())
        with self.assertRaises(FileExistsError):
            timer.wait(self.record)

    def test_guard_returns_only_matching_bounded_utf8_snapshot(self):
        self.start()
        result = timer.read_review(self.record, self.snapshot, self.digest)
        self.assertEqual(result["source"], "example source\n")
        self.assertEqual(result["sha256"], self.digest)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            timer.read_review(self.record, self.snapshot, "a" * 64)
        self.snapshot.write_bytes(b"x" * (timer.MAX_REVIEW_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "byte limit"):
            timer.read_review(self.record, self.snapshot, self.digest)

    def test_margin_prevents_even_opening_snapshot(self):
        self.start()
        self.clock.advance(20)
        result = timer.read_review(self.record, self.root / "absent", self.digest)
        self.assertFalse(result["review_allowed"])
        self.assertNotIn("source", result)

    def test_margin_crossed_during_read_withholds_content(self):
        self.start()
        original = timer.bounded_read

        def read(path, limit):
            raw = original(path, limit)
            if Path(path) == self.snapshot:
                self.clock.advance(21)
            return raw

        with patch.object(timer, "bounded_read", side_effect=read):
            result = timer.read_review(self.record, self.snapshot, self.digest)
        self.assertFalse(result["review_allowed"])
        self.assertNotIn("source", result)

    def test_cancellation_during_read_withholds_content(self):
        self.start()
        original = timer.bounded_read

        def read(path, limit):
            raw = original(path, limit)
            if Path(path) == self.snapshot:
                timer.cancel(self.record)
            return raw

        with patch.object(timer, "bounded_read", side_effect=read):
            result = timer.read_review(self.record, self.snapshot, self.digest)
        self.assertFalse(result["review_allowed"])
        self.assertNotIn("source", result)

    def test_cli_errors_are_bounded_and_do_not_create_state(self):
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(timer.main(["status", "--record", str(self.record)]), 2)
        self.assertIn("reconcile", json.loads(errors.getvalue())["action"])
        self.assertFalse(self.record.exists())
        with redirect_stdout(io.StringIO()):
            self.start()
            self.assertEqual(timer.main(["cancel", "--record", str(self.record)]), 0)


class StandaloneTests(unittest.TestCase):
    def test_script_runs_with_isolated_python_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = [sys.executable, "-I", str(SCRIPT)]
            binding = [item for key, value in BINDING.items()
                       for item in ("--" + key.replace("_", "-"), value)]

            def run(args, expected=0):
                result = subprocess.run([*prefix, *args], cwd=directory,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                return json.loads(result.stdout if expected == 0 else result.stderr)

            run(["start", "--record", "cycle", "--seconds", "0.001", *binding])
            outcome = run(["wait", "--record", "cycle"])
            self.assertTrue(outcome["due"])
            self.assertEqual(outcome["binding"], BINDING)
            run(["wait", "--record", "cycle"], expected=2)
            run(["cancel", "--record", "cycle"])
            self.assertFalse(run(["status", "--record", "cycle"])["due"])

    def test_unsupported_platform_requires_host_timing(self):
        with patch.object(timer.platform, "system", return_value="Unsupported"):
            with self.assertRaisesRegex(ValueError, "supports macOS and Linux"):
                timer.boot_identity()


if __name__ == "__main__":
    unittest.main()
