"""Project profiles and shared-skill use from unrelated working directories."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from codex_scheduler import Registry, SchedulerError
from codex_scheduler.__main__ import main as cli
from codex_scheduler.delivery import DeliveryStore
from codex_scheduler.project import Project
from codex_scheduler.runner import run_wrapper


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SKILL_HELPER = SOURCE_ROOT / "skills" / "bounded-workflow" / "scripts" / "scheduler.py"


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "scheduler.toml"
        self.profile.write_text('version = 1\n[commands]\nverify = ["python3", "-c", "print(45)"]\n')

    def call(self, *arguments):
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = cli(["--project", str(self.profile), *arguments])
        return code, json.loads(output.getvalue()) if output.getvalue() else error.getvalue()

    def grant(self):
        code, grant = self.call("create-grant", "--thread", "synthetic-worker", "--scope", "offline fixture")
        self.assertEqual(code, 0)
        return grant

    def job(self):
        grant = self.grant()
        code, job = self.call("register", "--grant", grant["grant_id"], "--command", "verify")
        self.assertEqual(code, 0)
        return grant, job

    def test_inspection_is_read_only_and_resolves_paths_from_the_profile(self):
        code, info = self.call("project-info")
        self.assertEqual(code, 0)
        self.assertEqual(Path(info["workspace"]), self.root.resolve())
        self.assertEqual(Path(info["state_dir"]), self.root.resolve() / ".codex-scheduler")
        self.assertFalse((self.root / ".codex-scheduler").exists())

    def test_invalid_profiles_are_rejected_before_state_creation(self):
        invalid = [
            'version = true\n', 'version = 2\n', 'version = 1\nautomatic = true\n',
            'version = 1\nstate_dir = ".."\n', 'version = 1\nstate_dir = "."\n',
            'version = 1\n[commands]\nverify = "python3 check.py"\n',
            'version = 1\n[commands]\nverify = []\n',
            'version = 1\n[limits]\nmax_jobs = true\n',
            'version = 1\n[limits]\ntimeout_seconds = nan\n',
            'version = 1\n[limits]\nmax_check = 10\n',
        ]
        for content in invalid:
            with self.subTest(content=content):
                self.profile.write_text(content)
                with self.assertRaises(SchedulerError):
                    Project.load(self.profile)
                self.assertFalse((self.root / ".codex-scheduler").exists())

    def test_state_symlink_cannot_escape_the_workspace(self):
        (self.root / ".codex-scheduler").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(SchedulerError):
            Project.load(self.profile)

    def test_oversized_profile_is_rejected(self):
        self.profile.write_text("#" * 65537)
        with self.assertRaises(SchedulerError):
            Project.load(self.profile)

    def test_new_grant_cannot_exceed_project_bounds(self):
        code, _ = self.call("create-grant", "--thread", "synthetic-worker", "--scope", "fixture", "--max-jobs", "2")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".codex-scheduler").exists())

    def test_explicit_workspace_cannot_override_selected_project(self):
        code, _ = self.call("create-grant", "--workspace", str(self.root.parent),
                            "--thread", "synthetic-worker", "--scope", "fixture")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".codex-scheduler").exists())

    def test_named_command_registration_does_not_execute_or_enable_delivery(self):
        grant, job = self.job()
        registry = Registry(self.root / ".codex-scheduler")
        self.assertEqual(job["command"], ["python3", "-c", "print(45)"])
        self.assertEqual(registry.inspect(job["job_id"])["observed_state"], "registered")
        self.assertEqual(registry.grant(grant["grant_id"])["consumed_jobs"], 0)
        with self.assertRaises(SchedulerError):
            DeliveryStore(registry).policy(grant["grant_id"])

    def test_unknown_or_mixed_commands_do_not_register_jobs(self):
        grant = self.grant()
        for arguments in (["--command", "missing"], ["--command", "verify", "--", "echo", "mixed"]):
            code, _ = self.call("register", "--grant", grant["grant_id"], *arguments)
            self.assertEqual(code, 2)
        registry = Registry(self.root / ".codex-scheduler")
        with registry.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_profile_changes_cannot_restore_a_consumed_job_allowance(self):
        grant, job = self.job()
        registry = Registry(self.root / ".codex-scheduler")
        registry.reserve_launch(job["job_id"])
        run_wrapper(registry, job["job_id"])
        self.profile.write_text('version = 1\n[limits]\nmax_jobs = 3\n')
        code, _ = self.call("register", "--grant", grant["grant_id"], "--", sys.executable, "-c", "print(2)")
        self.assertEqual(code, 2)
        self.assertEqual(registry.grant(grant["grant_id"])["consumed_jobs"], 1)

    def test_selected_project_rejects_a_foreign_workspace_in_its_registry(self):
        self.grant()
        registry = Registry(self.root / ".codex-scheduler")
        foreign = registry.create_grant(workspace=self.root.parent, worker_thread="other-worker", scope="other fixture")
        code, _ = self.call("grant-status", "--grant", foreign["grant_id"])
        self.assertEqual(code, 2)

    def test_delivery_bounds_come_from_profile_but_do_not_override_existing_policy(self):
        grant = self.grant()
        code, _ = self.call("configure-delivery", "--grant", grant["grant_id"],
                            "--socket", str(self.root / "worker.sock"), "--max-continuations", "2")
        self.assertEqual(code, 2)
        code, policy = self.call("configure-delivery", "--grant", grant["grant_id"],
                                 "--socket", str(self.root / "worker.sock"))
        self.assertEqual(code, 0)
        self.assertEqual(policy["spec"]["max_continuations"], 1)
        self.assertEqual(policy["consumed_continuations"], 0)

    def test_run_validates_before_launch_and_starts_waiter_in_the_same_call(self):
        grant, job = self.job()
        registry = Registry(self.root / ".codex-scheduler")
        store = DeliveryStore(registry)
        store.configure(grant["grant_id"], socket_path=self.root / "worker.sock")
        store.arm(job["job_id"], "Report result and stop")
        order = []
        with patch("codex_scheduler.__main__.launch", side_effect=lambda *_: order.append("launch")), \
                patch("codex_scheduler.delivery.Dispatcher.watch", side_effect=lambda *_, **__: order.append("watch") or {"state": "completed"}), \
                patch("codex_scheduler.codex.CodexAdapter.require_available"):
            code, result = self.call("run", "--job", job["job_id"])
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(order, ["launch", "watch"])
        with patch("codex_scheduler.__main__.launch") as start:
            code, _ = self.call("run", "--job", job["job_id"], "--interval", "0")
            self.assertEqual(code, 2)
            start.assert_not_called()

    def test_run_requires_an_armed_budget_and_transport_before_spawning(self):
        grant, job = self.job()
        with patch("codex_scheduler.__main__.launch") as start:
            code, _ = self.call("run", "--job", job["job_id"])
            self.assertEqual(code, 2)
            start.assert_not_called()
        registry = Registry(self.root / ".codex-scheduler")
        store = DeliveryStore(registry)
        store.configure(grant["grant_id"], socket_path=self.root / "worker.sock")
        store.arm(job["job_id"], "Report and stop")
        with patch("codex_scheduler.codex.CodexAdapter.require_available", side_effect=SchedulerError("missing transport")), \
                patch("codex_scheduler.__main__.launch") as start:
            code, _ = self.call("run", "--job", job["job_id"])
            self.assertEqual(code, 2)
            start.assert_not_called()
        self.assertEqual(registry.grant(grant["grant_id"])["consumed_jobs"], 0)

    def test_run_executes_the_real_job_and_delivers_once_with_a_simulated_worker(self):
        grant, job = self.job()
        registry = Registry(self.root / ".codex-scheduler")
        store = DeliveryStore(registry)
        store.configure(grant["grant_id"], socket_path=self.root / "worker.sock")
        store.arm(job["job_id"], "Report and stop")
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.snapshot.return_value = {
            "cwd": str(self.root.resolve()), "sandbox": {"type": "readOnly", "networkAccess": False},
            "approvalPolicy": "on-request", "approvalsReviewer": "user",
            "thread": {"id": "synthetic-worker", "status": {"type": "idle"}},
        }
        worker.has_queued_input.return_value = False
        worker.start.return_value = "synthetic-turn"
        worker.find_event.return_value = [{"id": "synthetic-turn", "status": "completed"}]
        with patch("codex_scheduler.codex.CodexAdapter", return_value=worker) as adapter:
            code, result = self.call("run", "--job", job["job_id"], "--interval", "0.05")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(registry.inspect(job["job_id"])["observed_state"], "succeeded")
        self.assertEqual(registry.grant(grant["grant_id"])["consumed_jobs"], 1)
        self.assertEqual(store.policy(grant["grant_id"])["consumed_continuations"], 1)
        worker.start.assert_called_once()
        adapter.require_available.assert_called_once()


class SharedSkillTests(unittest.TestCase):
    def test_two_projects_use_the_same_skill_and_runtime_with_separate_state(self):
        with tempfile.TemporaryDirectory(prefix="shared-skill-") as temporary:
            root = Path(temporary)
            caller = root / "unrelated caller"
            caller.mkdir()
            first = root / "arithmetic project"
            second = root / "catalog project"
            first.mkdir()
            second.mkdir()
            (first / "verify.py").write_text("assert sum(range(10)) == 45\nprint('arithmetic verified')\n")
            (second / "catalog.json").write_text('{"items": ["alpha", "beta"]}')
            (second / "check_catalog.py").write_text(
                "import json\nfrom pathlib import Path\n"
                "assert len(json.loads(Path('catalog.json').read_text())['items']) == 2\n"
                "print('catalog verified')\n"
            )
            profiles = []
            for folder, name, script in ((first, "verify", "verify.py"), (second, "audit", "check_catalog.py")):
                profile = folder / "scheduler.toml"
                profile.write_text('version = 1\n[commands]\n' + name + ' = ' + json.dumps([sys.executable, script]) + '\n')
                profiles.append((folder, profile, name))

            def invoke(profile, *arguments, ok=True):
                result = subprocess.run(
                    [sys.executable, str(SKILL_HELPER), "--runtime", str(SOURCE_ROOT), "--",
                     "--project", str(profile), *arguments],
                    cwd=caller, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
                return json.loads(result.stdout) if ok else None

            jobs = []
            for folder, profile, name in profiles:
                info = invoke(profile, "project-info")
                self.assertEqual(Path(info["workspace"]), folder.resolve())
                self.assertFalse((folder / ".codex-scheduler").exists())
                grant = invoke(profile, "create-grant", "--thread", "synthetic-" + name, "--scope", "one offline fixture")
                job = invoke(profile, "register", "--grant", grant["grant_id"], "--command", name)
                invoke(profile, "launch", "--job", job["job_id"])
                registry = Registry(folder / ".codex-scheduler")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = registry.inspect(job["job_id"])
                    if result["observed_state"] == result["recorded_state"] == "succeeded":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Shared-runtime job did not complete")
                status = invoke(profile, "status", "--job", job["job_id"])
                self.assertEqual(status["observed_state"], "succeeded")
                expected = "arithmetic verified" if name == "verify" else "catalog verified"
                self.assertEqual(Path(status["log_path"]).read_text().strip(), expected)
                jobs.append(job)
            self.assertNotEqual(jobs[0]["workspace_id"], jobs[1]["workspace_id"])
            self.assertNotEqual(jobs[0]["grant_id"], jobs[1]["grant_id"])
            invoke(profiles[1][1], "status", "--job", jobs[0]["job_id"], ok=False)
            invoke(profiles[0][1], "delivery-policy", "--grant", jobs[0]["grant_id"], ok=False)
            self.assertFalse((caller / ".codex-scheduler").exists())

    def test_launcher_requires_an_explicit_runtime_and_preserves_runtime_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run([sys.executable, str(SKILL_HELPER), "--runtime", str(root), "--", "--help"],
                                    cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((root / "state").exists())
            result = subprocess.run([sys.executable, str(SKILL_HELPER), "--runtime", str(SOURCE_ROOT), "--", "--help"],
                                    cwd=root, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertIn("project-info", result.stdout)

    def test_launcher_does_not_parse_child_arguments_as_bootstrap_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "scheduler.toml"
            profile.write_text("version = 1\n")
            prefix = [sys.executable, str(SKILL_HELPER), "--runtime", str(SOURCE_ROOT), "--",
                      "--project", str(profile)]
            grant_result = subprocess.run(prefix + ["create-grant", "--thread", "synthetic-worker", "--scope", "offline fixture"],
                                          cwd=root, capture_output=True, text=True, check=True, timeout=5)
            grant = json.loads(grant_result.stdout)
            command = [sys.executable, "-c", "import sys; print(sys.argv[1:])", "--runtime", "literal-value"]
            result = subprocess.run(prefix + ["register", "--grant", grant["grant_id"], "--", *command],
                                    cwd=root, capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(json.loads(result.stdout)["command"], command)


if __name__ == "__main__":
    unittest.main()
