"""Launch one registered job or run its detached wrapper. No agent calls."""

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time

from .registry import Registry, SchedulerError
from .storage import atomic_json, exclusive_lease, utc_now


def launch(registry, job_id):
    """Reserve first; a lost or failed spawn is never retried automatically."""
    registry.reserve_launch(job_id)
    directory = registry.job_directory(job_id)
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    try:
        with (directory / "wrapper.log").open("ab") as log:
            wrapper = subprocess.Popen(
                [sys.executable, "-m", "codex_scheduler.runner", "--state-dir", str(registry.root),
                 "--job", job_id],
                cwd=registry.root, env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
    except OSError as error:
        atomic_json(directory / "launch-error.json", {"error": str(error), "recorded_at": utc_now()})
        raise SchedulerError("Wrapper launch failed; the attempt remains consumed") from error
    # A long-lived caller reaps its child without blocking or asking an agent to poll.
    threading.Thread(target=wrapper.wait, daemon=True).start()
    return {"job_id": job_id, "state": "launching", "wrapper_pid": wrapper.pid}


def kill_group(process):
    """Only signal a process group created by this live wrapper, never a saved PID."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(registry, job):
    registration = job["registration"]
    directory = registry.job_directory(job["id"])
    limit = registration["max_log_bytes"]
    deadline = time.monotonic() + registration["timeout_seconds"]
    process = None
    size = 0
    outcome = None
    drain_deadline = None
    with (directory / "output.log").open("wb") as log:
        try:
            try:
                process = subprocess.Popen(
                    registration["command"], cwd=registration["workspace"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            except OSError as error:
                message = str(error).encode()[:limit]
                log.write(message)
                return "failed", 127, len(message)
            registry.record_command_pid(job["id"], process.pid)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    current = time.monotonic()
                    if outcome is None and current >= deadline:
                        outcome = "timed_out"
                        kill_group(process)
                        drain_deadline = current + 1
                    if drain_deadline is not None and current >= drain_deadline:
                        break
                    for key, _ in selector.select(timeout=0.05):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        retained = data[:max(0, limit - size)]
                        log.write(retained)
                        size += len(retained)
                        if len(data) > len(retained) and outcome is None:
                            outcome = "output_limit"
                            kill_group(process)
                            drain_deadline = time.monotonic() + 1
            # A command can close stdout yet keep running. The timeout still applies.
            if outcome is None:
                try:
                    code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    outcome = "timed_out"
                    kill_group(process)
            code = process.wait(timeout=2)
            return outcome or ("succeeded" if code == 0 else "failed"), code, size
        finally:
            if process is not None:
                if process.poll() is None:
                    kill_group(process)
                    process.wait(timeout=2)
                process.stdout.close()
            log.flush()
            os.fsync(log.fileno())


def run_wrapper(registry, job_id):
    directory = registry.job_directory(job_id)
    with exclusive_lease(directory / "wrapper.lock"):
        registry.claim_wrapper(job_id, os.getpid())
        job = registry.job(job_id)
        registration = job["registration"]
        # A missing or altered registration cannot silently become a different job.
        if json.loads((directory / "registration.json").read_text()) != registration:
            raise SchedulerError("Saved registration differs from the registry")
        outcome, code, size = execute(registry, job)
        receipt = {
            key: registration[key] for key in (
                "schema_version", "job_id", "grant_id", "workspace_id", "worker_thread",
                "launch_token", "registration_hash",
            )
        }
        receipt.update(outcome=outcome, exit_code=code, log_bytes=size,
                       started_at=job["started_at"], completed_at=utc_now())
        # This ordering allows a new controller to reconcile a receipt after a crash.
        atomic_json(directory / "completion.json", receipt)
        return registry.recover(job_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    try:
        run_wrapper(Registry(args.state_dir), args.job)
    except (SchedulerError, OSError, ValueError) as error:
        print(f"Wrapper stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
