"""Run one approved synthetic job through the durable runner and dispatcher."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from codex_scheduler import Registry
from codex_scheduler.codex import CodexAdapter
from codex_scheduler.delivery import DeliveryStore, Dispatcher
from codex_scheduler.registry import TERMINAL
from codex_scheduler.runner import launch
from prototypes.worker_lifecycle import Harness, Trial, TrialError, now, save


class RegisteredJob:
    def __init__(self, registry, job_id):
        self.registry = registry
        self.job_id = job_id

    def poll(self):
        result = self.registry.inspect(self.job_id)
        if result["observed_state"] in TERMINAL:
            return result["receipt"]["exit_code"]
        if result["observed_state"] == "running":
            return None
        raise TrialError("Registered job has no live wrapper evidence")

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.poll()
            if code is not None:
                return code
            time.sleep(0.05)
        raise TrialError("Registered job did not finish within the trial allowance")


class DurableHarness(Harness):
    def before_initial_turn(self, configuration):
        self.registry = Registry(self.trial.directory / "runtime", create=True)
        grant = self.registry.create_grant(
            workspace=configuration["cwd"], worker_thread=configuration["thread"]["id"],
            scope="One harmless synthetic job and one context-recall continuation",
            max_jobs=1, timeout_seconds=self.timeout, max_log_bytes=1024,
        )
        self.store = DeliveryStore(self.registry)
        self.store.configure(
            grant["grant_id"], socket_path=self.socket_path, max_continuations=1, max_checks=10,
            permissions={key: configuration[key] for key in ("sandbox", "approvalPolicy", "approvalsReviewer")},
        )
        command = [sys.executable, str(Path(__file__).with_name("synthetic_job.py")),
                   "--release", str(self.trial.directory / "release"), "--timeout", str(self.timeout)]
        registration = self.registry.register(grant["grant_id"], command)
        self.job_id = registration["job_id"]
        self.store.arm(
            self.job_id,
            "The receipt is verified. Reply exactly CONTINUED:<remembered context token>:<job_id>:<exit_code> "
            "using the event fields, then stop. Do not call tools.",
        )
        self.dispatcher = Dispatcher(self.registry, CodexAdapter)
        self.trial.write(job_id=self.job_id, grant_id=grant["grant_id"],
                         runtime_state_dir=str(self.registry.root))

    def start_job(self):
        self.trial.reserve("job")
        info = launch(self.registry, self.job_id)
        self.job = RegisteredJob(self.registry, self.job_id)
        self.trial.write(phase="job_running", wrapper_pid=info["wrapper_pid"])

    def finish_job(self):
        code = self.job.wait(self.timeout)
        result = self.registry.recover(self.job_id)
        if code != 0 or result["observed_state"] != "succeeded":
            raise TrialError("The durable synthetic job did not succeed")
        output = Path(result["log_path"]).read_text()
        if json.loads(output) != {"result": 45, "status": "completed"}:
            raise TrialError("Synthetic job produced unexpected evidence")
        receipt = {"job_id": self.job_id, "exit_code": code, "result": 45,
                   "completed_at": result["receipt"]["completed_at"]}
        save(self.trial.directory / "completion.json", receipt)
        return receipt

    def deliver_completion(self, rpc, thread_id, receipt):
        self.trial.reserve("continuation")
        self.trial.write(dispatch_started_at=now())
        event = self.dispatcher.step(self.job_id)
        self.trial.write(delivery_event_id=event["event_id"], delivery_state=event["state"])
        if event["state"] != "started":
            raise TrialError("Durable delivery did not start: " + str(event["reason"]))
        completed = rpc.completed_turn(thread_id, event["turn_id"])
        event = self.dispatcher.step(self.job_id, reconcile_only=True)
        policy = self.store.policy(event["grant_id"])
        self.trial.write(delivery_state=event["state"],
                         consumed_continuations=policy["consumed_continuations"],
                         consumed_checks=policy["consumed_checks"])
        if event["state"] != "completed" or policy["consumed_continuations"] != 1:
            raise TrialError("Durable delivery did not reconcile to completion")
        return completed

    def cleanup_job(self):
        if self.job:
            # Release only this known harmless fixture; never signal a stored process ID.
            (self.trial.directory / "release").touch(exist_ok=True)
            try:
                self.job.wait(3)
            except TrialError:
                self.trial.write(job_cleanup_unknown=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--retry-bootstrap", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    trial = Trial(args.run_dir, "durable", args.retry_bootstrap)
    try:
        version = subprocess.run([args.codex, "--version"], capture_output=True, text=True,
                                 check=True, timeout=10).stdout.strip()
        trial.write(codex_version=version)
        DurableHarness(trial, args.codex, args.timeout).run(args.preflight_only)
    except Exception as error:
        trial.write(stopped_phase=trial.state["phase"], phase="stopped", stopped_at=now(), error=str(error))
        print(f"Trial stopped. Inspect {trial.directory / 'status.json'}", file=sys.stderr)
        return 1
    label = "Preflight" if args.preflight_only else "Durable trial"
    print(f"{label} passed. Inspect {trial.directory / 'status.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
