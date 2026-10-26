"""Job and recovery behavior without Codex or network access."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import gc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import warnings

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.runner import launch, run_wrapper
from codex_scheduler.storage import atomic_json, exclusive_lease


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.registry = Registry(self.state, create=True)

    def grant(self, **kwargs):
        return self.registry.create_grant(
            workspace=self.workspace, worker_thread="synthetic-worker", scope="offline fixture", **kwargs,
        )

    def register(self, script="print(45)", **kwargs):
        grant = self.grant(**kwargs)
        job = self.registry.register(grant["grant_id"], [sys.executable, "-c", script])
        return grant, job

    def execute(self, script="print(45)", **kwargs):
        grant, registration = self.register(script, **kwargs)
        self.registry.reserve_launch(registration["job_id"])
        result = run_wrapper(self.registry, registration["job_id"])
        return grant, registration, result

    def test_registration_is_durable_before_launch_and_has_distinct_owners(self):
        grant, job = self.register()
        reopened = Registry(self.state)
        self.assertEqual(reopened.job(job["job_id"])["registration"], job)
        saved = json.loads((reopened.job_directory(job["job_id"]) / "registration.json").read_text())
        self.assertEqual(saved, job)
        self.assertEqual(reopened.inspect(job["job_id"])["observed_state"], "registered")
        self.assertEqual(reopened.grant(grant["grant_id"])["consumed_jobs"], 0)
        self.assertEqual(len({job["job_id"], job["grant_id"], job["workspace_id"], job["launch_token"]}), 4)
        self.assertEqual(job["worker_thread"], "synthetic-worker")

    def test_success_has_a_bound_completion_and_bounded_status(self):
        grant, job, result = self.execute()
        self.assertEqual(result["observed_state"], "succeeded")
        self.assertEqual(result["recorded_state"], "succeeded")
        receipt = result["receipt"]
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["registration_hash"], job["registration_hash"])
        self.assertEqual(receipt["grant_id"], grant["grant_id"])
        self.assertEqual(Path(result["log_path"]).read_text(), "45\n")
        self.assertNotIn("output", result)

    def test_nonzero_exit_is_failure_with_its_actual_code(self):
        _, _, result = self.execute("import sys; print('fixture failure'); sys.exit(7)")
        self.assertEqual(result["observed_state"], "failed")
        self.assertEqual(result["receipt"]["exit_code"], 7)

    def test_missing_executable_consumes_attempt_and_produces_failure(self):
        grant = self.grant()
        job = self.registry.register(grant["grant_id"], [str(self.workspace / "missing-program")])
        self.registry.reserve_launch(job["job_id"])
        result = run_wrapper(self.registry, job["job_id"])
        self.assertEqual(result["observed_state"], "failed")
        self.assertEqual(result["receipt"]["exit_code"], 127)
        self.assertEqual(self.registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_timeout_is_distinct_from_command_failure(self):
        _, _, result = self.execute("import time; time.sleep(10)", timeout_seconds=0.1)
        self.assertEqual(result["observed_state"], "timed_out")
        self.assertLess(result["receipt"]["exit_code"], 0)

    def test_closing_output_does_not_remove_the_runtime_limit(self):
        _, _, result = self.execute(
            "import os, time; os.close(1); os.close(2); time.sleep(10)", timeout_seconds=0.1,
        )
        self.assertEqual(result["observed_state"], "timed_out")

    def test_excess_output_is_cut_off_at_the_granted_limit(self):
        _, _, result = self.execute("print('x' * 10000)", max_log_bytes=31)
        self.assertEqual(result["observed_state"], "output_limit")
        self.assertEqual(Path(result["log_path"]).stat().st_size, 31)
        self.assertEqual(result["receipt"]["log_bytes"], 31)

    def test_command_arguments_are_not_interpreted_by_a_shell(self):
        grant = self.grant()
        marker = self.workspace / "must-not-exist"
        literal = f"$(touch {marker})"
        job = self.registry.register(grant["grant_id"], [sys.executable, "-c", "import sys; print(sys.argv[1])", literal])
        self.registry.reserve_launch(job["job_id"])
        result = run_wrapper(self.registry, job["job_id"])
        self.assertEqual(result["observed_state"], "succeeded")
        self.assertEqual(Path(result["log_path"]).read_text().strip(), literal)
        self.assertFalse(marker.exists())

    def test_reopening_or_initializing_does_not_restore_consumed_allowance(self):
        grant, job, _ = self.execute()
        reopened = Registry(self.state, create=True)
        self.assertEqual(reopened.grant(grant["grant_id"])["consumed_jobs"], 1)
        with self.assertRaises(SchedulerError):
            reopened.register(grant["grant_id"], [sys.executable, "-c", "print(1)"])
        with self.assertRaises(SchedulerError):
            reopened.reserve_launch(job["job_id"])

    def test_competing_launches_consume_only_one_attempt(self):
        grant, job = self.register(max_jobs=2)

        def reserve():
            try:
                Registry(self.state).reserve_launch(job["job_id"])
                return "reserved"
            except SchedulerError:
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reserve(), range(2)))
        self.assertCountEqual(results, ["reserved", "refused"])
        self.assertEqual(self.registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_second_grant_cannot_overlap_an_unresolved_workspace(self):
        _, job = self.register()
        second = self.grant()
        with self.assertRaises(SchedulerError):
            self.registry.register(second["grant_id"], [sys.executable, "-c", "print(1)"])
        self.registry.reserve_launch(job["job_id"])
        self.registry.recover(job["job_id"])
        with self.assertRaises(SchedulerError):
            self.registry.register(second["grant_id"], [sys.executable, "-c", "print(1)"])

    def test_reservation_survives_lost_launcher_without_relaunch(self):
        grant, job = self.register()
        self.registry.reserve_launch(job["job_id"])
        with patch("subprocess.Popen") as start:
            result = Registry(self.state).recover(job["job_id"])
            self.assertEqual(result["observed_state"], "unknown")
            with self.assertRaises(SchedulerError):
                launch(Registry(self.state), job["job_id"])
            start.assert_not_called()
        self.assertEqual(self.registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_failed_wrapper_spawn_cannot_be_retried(self):
        grant, job = self.register()
        with patch("codex_scheduler.runner.subprocess.Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaises(SchedulerError):
                launch(self.registry, job["job_id"])
        self.assertEqual(Registry(self.state).inspect(job["job_id"])["observed_state"], "unknown")
        self.assertEqual(self.registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_a_reused_pid_without_its_wrapper_lease_is_unknown(self):
        _, job = self.register()
        self.registry.reserve_launch(job["job_id"])
        self.registry.claim_wrapper(job["job_id"], os.getpid())
        self.registry.record_command_pid(job["job_id"], os.getpid())
        self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")
        with exclusive_lease(self.registry.job_directory(job["job_id"]) / "wrapper.lock"):
            self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "running")
        self.assertEqual(self.registry.recover(job["job_id"])["observed_state"], "unknown")

    def test_a_duplicate_wrapper_cannot_execute_the_command(self):
        _, job, _ = self.execute()
        with patch("codex_scheduler.runner.execute") as execute:
            with self.assertRaises(SchedulerError):
                run_wrapper(self.registry, job["job_id"])
            execute.assert_not_called()

    def test_altered_registration_never_reaches_command_execution(self):
        _, job = self.register()
        self.registry.reserve_launch(job["job_id"])
        path = self.registry.job_directory(job["job_id"]) / "registration.json"
        atomic_json(path, {**job, "command": ["unexpected-program"]})
        with patch("codex_scheduler.runner.execute") as execute:
            with self.assertRaises(SchedulerError):
                run_wrapper(self.registry, job["job_id"])
            execute.assert_not_called()
        self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")

    def test_missing_receipt_never_reuses_an_old_success(self):
        _, job, _ = self.execute()
        (self.registry.job_directory(job["job_id"]) / "completion.json").unlink()
        result = Registry(self.state).recover(job["job_id"])
        self.assertEqual(result["observed_state"], "unknown")
        self.assertIsNone(result["receipt"])

    def test_stale_foreign_malformed_and_symlink_receipts_are_unknown(self):
        _, job, result = self.execute()
        path = self.registry.job_directory(job["job_id"]) / "completion.json"
        original = result["receipt"]
        for key in ("launch_token", "job_id", "workspace_id", "worker_thread", "registration_hash"):
            with self.subTest(key=key):
                atomic_json(path, {**original, key: "other-launch"})
                self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")
        path.write_text("{incomplete")
        self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")
        other = self.root / "other-receipt.json"
        atomic_json(other, original)
        path.unlink()
        path.symlink_to(other)
        self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")

    def test_receipt_recovers_after_completion_preceded_registry_commit(self):
        grant, job, _ = self.execute()
        with self.registry.connect(write=True) as connection:
            connection.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job["job_id"],))
        with patch("subprocess.Popen") as start:
            result = Registry(self.state).recover(job["job_id"])
            start.assert_not_called()
        self.assertEqual(result["recorded_state"], "succeeded")
        self.assertEqual(result["observed_state"], "succeeded")
        self.assertEqual(self.registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_malformed_receipt_fields_cannot_establish_success(self):
        _, job, result = self.execute()
        path = self.registry.job_directory(job["job_id"]) / "completion.json"
        for values in ({"exit_code": True}, {"exit_code": 7}, {"schema_version": 99}, {"schema_version": True},
                       {"log_bytes": -1}, {"log_bytes": 1048577}, {"completed_at": None}):
            with self.subTest(values=values):
                atomic_json(path, {**result["receipt"], **values})
                self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "unknown")

    def test_lost_old_receipt_does_not_reactivate_over_a_successor(self):
        grant, first, _ = self.execute(max_jobs=2)
        second = self.registry.register(grant["grant_id"], [sys.executable, "-c", "print(2)"])
        (self.registry.job_directory(first["job_id"]) / "completion.json").unlink()
        self.assertEqual(self.registry.recover(first["job_id"])["observed_state"], "unknown")
        self.assertEqual(self.registry.inspect(second["job_id"])["observed_state"], "registered")

    def test_detached_wrapper_survives_the_launching_cli_and_controller_reopen(self):
        script = (
            "from pathlib import Path\nimport time\n"
            "while not Path('release').exists():\n    time.sleep(0.01)\nprint(45)"
        )
        grant, job = self.register(script, timeout_seconds=5)
        command = [sys.executable, "-m", "codex_scheduler", "--state-dir", str(self.state),
                   "launch", "--job", job["job_id"]]
        parent = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(json.loads(parent.stdout)["state"], "launching")
        reopened = Registry(self.state)
        deadline = time.monotonic() + 5
        release = self.workspace / "release"
        try:
            while reopened.inspect(job["job_id"])["observed_state"] != "running":
                if time.monotonic() >= deadline:
                    self.fail("Wrapper did not survive its launching CLI")
                time.sleep(0.01)
            self.assertFalse(release.exists())
        finally:
            release.touch()
        while time.monotonic() < deadline:
            result = reopened.inspect(job["job_id"])
            if result["observed_state"] == result["recorded_state"] == "succeeded":
                break
            time.sleep(0.02)
        else:
            self.fail("Detached wrapper did not commit its completion receipt")
        self.assertEqual(reopened.recover(job["job_id"])["recorded_state"], "succeeded")
        self.assertEqual(reopened.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_invalid_bounds_and_path_traversal_are_rejected(self):
        for values in ({"max_jobs": True}, {"max_jobs": 0}, {"timeout_seconds": float("nan")},
                       {"timeout_seconds": 0}, {"max_log_bytes": -1}):
            with self.subTest(values=values), self.assertRaises(SchedulerError):
                self.grant(**values)
        with self.assertRaises(SchedulerError):
            self.registry.inspect("../not-a-job")

    def test_status_is_read_only_and_cli_does_not_dump_output(self):
        _, job, _ = self.execute()
        before = self.registry.database.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli(["--state-dir", str(self.state), "status", "--job", job["job_id"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["observed_state"], "succeeded")
        self.assertEqual(before, self.registry.database.read_bytes())
        absent = self.root / "absent"
        with self.assertRaises(SchedulerError):
            Registry(absent)
        self.assertFalse(absent.exists())

    def test_unsupported_registry_version_is_preserved(self):
        with self.registry.connect(write=True) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(SchedulerError):
            Registry(self.state, create=True)
        with self.registry.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_registry_initialization_closes_its_database_connection(self):
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            Registry(self.state, create=True)
            gc.collect()
        self.assertEqual([warning for warning in caught if warning.category is ResourceWarning], [])

    def test_cli_grant_and_registration_commands_match_the_documented_flow(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli(["--state-dir", str(self.state), "create-grant",
                        "--workspace", str(self.workspace), "--thread", "synthetic-worker",
                        "--scope", "one offline fixture", "--timeout", "30"])
        self.assertEqual(code, 0)
        grant = json.loads(output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli(["--state-dir", str(self.state), "register", "--grant", grant["grant_id"],
                        "--", sys.executable, "-c", "print(45)"])
        self.assertEqual(code, 0)
        job = json.loads(output.getvalue())
        self.assertEqual(job["command"], [sys.executable, "-c", "print(45)"])
        self.assertEqual(self.registry.inspect(job["job_id"])["observed_state"], "registered")


if __name__ == "__main__":
    unittest.main()
