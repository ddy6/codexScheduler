"""A durable, single-host registry. Inspection never launches work."""

from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from .storage import atomic_json, canonical, lease_is_held, utc_now


class SchedulerError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    spec TEXT NOT NULL,
    consumed_jobs INTEGER NOT NULL DEFAULT 0 CHECK(consumed_jobs >= 0)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES grants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    registration TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('registered','launching','running','unknown','succeeded','failed','timed_out','output_limit')),
    launch_consumed INTEGER NOT NULL DEFAULT 0,
    wrapper_claimed INTEGER NOT NULL DEFAULT 0,
    wrapper_pid INTEGER,
    command_pid INTEGER,
    started_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_workspace ON jobs(workspace_id)
    WHERE state IN ('registered','launching','running','unknown');
"""

DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS continuation_grants (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id),
    spec TEXT NOT NULL,
    consumed_continuations INTEGER NOT NULL DEFAULT 0 CHECK(consumed_continuations >= 0),
    consumed_checks INTEGER NOT NULL DEFAULT 0 CHECK(consumed_checks >= 0),
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    grant_id TEXT NOT NULL REFERENCES grants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    worker_thread TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('pending','dispatching','uncertain','started','completed','failed','blocked','revoked')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts IN (0, 1)),
    request_text TEXT,
    receipt_hash TEXT,
    turn_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_delivery_per_worker ON deliveries(worker_thread)
    WHERE state IN ('pending','dispatching','uncertain','started','blocked');
CREATE UNIQUE INDEX IF NOT EXISTS one_delivery_per_workspace ON deliveries(workspace_id)
    WHERE state IN ('pending','dispatching','uncertain','started','blocked');
"""

TERMINAL = {"succeeded", "failed", "timed_out", "output_limit"}

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_waits (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    source_turn_id TEXT NOT NULL,
    quota TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_resumes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    grant_id TEXT NOT NULL REFERENCES grants(id),
    source_turn_id TEXT NOT NULL,
    request_text TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('dispatching','started','uncertain','completed','failed')),
    turn_id TEXT,
    UNIQUE(job_id, source_turn_id)
);
"""


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise SchedulerError("Expected a registry identity (32 lowercase hexadecimal characters)")
    return value


def nonempty(value, name, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise SchedulerError(f"{name} must be nonempty text, at most {maximum} characters")
    return value


class Registry:
    def __init__(self, root, *, create=False):
        self.root = Path(root).resolve()
        self.database = self.root / "registry.sqlite3"
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.root / "jobs").mkdir(exist_ok=True, mode=0o700)
            with closing(sqlite3.connect(self.database)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise SchedulerError("Unsupported registry version")
                if version < 3:
                    connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + DELIVERY_SCHEMA + USAGE_SCHEMA
                                             + "PRAGMA user_version = 3;\nCOMMIT;")
            self.database.chmod(0o600)
        if not self.database.is_file():
            raise SchedulerError("Registry does not exist; create an explicit task grant first")
        with self.connect() as connection:
            self.version = connection.execute("PRAGMA user_version").fetchone()[0]
            if self.version not in (1, 2, 3):
                raise SchedulerError("Unsupported registry version")

    @contextmanager
    def connect(self, *, write=False):
        uri = self.database.as_uri() + ("?mode=rw" if write else "?mode=ro")
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def create_grant(self, *, workspace, worker_thread, scope, max_jobs=1,
                     timeout_seconds=60, max_log_bytes=1048576):
        workspace = Path(workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise SchedulerError("Workspace must be a directory")
        nonempty(worker_thread, "Worker thread", 256)
        if worker_thread in {"--last", "last"}:
            raise SchedulerError("An exact worker thread is required")
        nonempty(scope, "Task scope")
        if type(max_jobs) is not int or not 1 <= max_jobs <= 1000:
            raise SchedulerError("max_jobs must be an integer between 1 and 1000")
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 86400):
            raise SchedulerError("timeout_seconds must be positive and no more than 86400")
        if type(max_log_bytes) is not int or not 1 <= max_log_bytes <= 104857600:
            raise SchedulerError("max_log_bytes must be between 1 and 104857600")
        grant_id = uuid.uuid4().hex
        with self.connect(write=True) as connection:
            row = connection.execute("SELECT id FROM workspaces WHERE path = ?", (str(workspace),)).fetchone()
            workspace_id = row["id"] if row else uuid.uuid4().hex
            if row is None:
                connection.execute("INSERT INTO workspaces VALUES (?, ?)", (workspace_id, str(workspace)))
            spec = {
                "grant_id": grant_id, "workspace_id": workspace_id, "workspace": str(workspace),
                "worker_thread": worker_thread, "scope": scope, "max_jobs": max_jobs,
                "timeout_seconds": timeout_seconds, "max_log_bytes": max_log_bytes,
                "created_at": utc_now(),
            }
            connection.execute("INSERT INTO grants(id, workspace_id, spec) VALUES (?, ?, ?)",
                               (grant_id, workspace_id, canonical(spec)))
        return {**spec, "consumed_jobs": 0}

    def grant(self, grant_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM grants WHERE id = ?", (identity(grant_id),)).fetchone()
        if row is None:
            raise SchedulerError("Unknown grant")
        return {**json.loads(row["spec"]), "consumed_jobs": row["consumed_jobs"]}

    def job_directory(self, job_id):
        return self.root / "jobs" / identity(job_id)

    def job(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (identity(job_id),)).fetchone()
        if row is None:
            raise SchedulerError("Unknown job")
        return {**dict(row), "registration": json.loads(row["registration"])}

    def register(self, grant_id, command):
        if (not isinstance(command, (list, tuple)) or not command or len(command) > 256
                or any(not isinstance(arg, str) or "\0" in arg for arg in command)
                or not command[0] or sum(len(arg) for arg in command) > 65536):
            raise SchedulerError("Command must be a nonempty argv list, at most 64 KiB")
        grant = self.grant(grant_id)
        job_id = uuid.uuid4().hex
        registration = {
            "schema_version": 1, "job_id": job_id, "grant_id": grant_id,
            "workspace_id": grant["workspace_id"], "workspace": grant["workspace"],
            "worker_thread": grant["worker_thread"], "scope": grant["scope"],
            "command": list(command), "timeout_seconds": grant["timeout_seconds"],
            "max_log_bytes": grant["max_log_bytes"], "launch_token": uuid.uuid4().hex,
            "created_at": utc_now(),
        }
        registration["registration_hash"] = hashlib.sha256(canonical(registration).encode()).hexdigest()
        try:
            with self.connect(write=True) as connection:
                if self.version >= 2 and connection.execute(
                    "SELECT 1 FROM deliveries WHERE workspace_id = ? "
                    "AND state IN ('pending','dispatching','uncertain','started','blocked')",
                    (grant["workspace_id"],),
                ).fetchone():
                    raise SchedulerError("Workspace has a pending or unresolved continuation")
                row = connection.execute("SELECT consumed_jobs FROM grants WHERE id = ?", (grant_id,)).fetchone()
                if row["consumed_jobs"] >= grant["max_jobs"]:
                    raise SchedulerError("Task launch allowance exhausted")
                connection.execute(
                    "INSERT INTO jobs(id, grant_id, workspace_id, registration, state) VALUES (?, ?, ?, ?, 'registered')",
                    (job_id, grant_id, grant["workspace_id"], canonical(registration)),
                )
                directory = self.job_directory(job_id)
                directory.mkdir(mode=0o700)
                atomic_json(directory / "registration.json", registration)
        except sqlite3.IntegrityError as error:
            raise SchedulerError("Workspace already has an active or unresolved job") from error
        return registration

    def reserve_launch(self, job_id):
        job_id = identity(job_id)
        with self.connect(write=True) as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None or job["state"] != "registered" or job["launch_consumed"]:
                raise SchedulerError("Job cannot be launched again")
            grant = connection.execute("SELECT * FROM grants WHERE id = ?", (job["grant_id"],)).fetchone()
            if grant["consumed_jobs"] >= json.loads(grant["spec"])["max_jobs"]:
                raise SchedulerError("Task launch allowance exhausted")
            connection.execute("UPDATE grants SET consumed_jobs = consumed_jobs + 1 WHERE id = ?", (grant["id"],))
            connection.execute("UPDATE jobs SET launch_consumed = 1, state = 'launching' WHERE id = ?", (job_id,))

    def claim_wrapper(self, job_id, wrapper_pid):
        with self.connect(write=True) as connection:
            result = connection.execute(
                "UPDATE jobs SET wrapper_claimed = 1, wrapper_pid = ?, state = 'running', started_at = ? "
                "WHERE id = ? AND state = 'launching' AND launch_consumed = 1 AND wrapper_claimed = 0",
                (wrapper_pid, utc_now(), identity(job_id)),
            )
            if result.rowcount != 1:
                raise SchedulerError("Wrapper already claimed or job requires reconciliation")

    def record_command_pid(self, job_id, command_pid):
        with self.connect(write=True) as connection:
            connection.execute("UPDATE jobs SET command_pid = ? WHERE id = ? AND wrapper_claimed = 1",
                               (command_pid, identity(job_id)))

    def _receipt(self, job):
        path = self.job_directory(job["id"]) / "completion.json"
        try:
            if path.is_symlink():
                raise ValueError("Receipt is a symlink")
            with path.open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError("Receipt is oversized")
            receipt = json.loads(data)
            registration = job["registration"]
            for key in ("job_id", "grant_id", "workspace_id", "worker_thread", "launch_token", "registration_hash"):
                if receipt[key] != registration[key]:
                    raise ValueError("Receipt does not match this launch")
            if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                    or not job["launch_consumed"] or not job["wrapper_claimed"]):
                raise ValueError("Receipt has no registered launch")
            if (receipt["outcome"] not in TERMINAL or type(receipt["exit_code"]) is not int
                    or type(receipt["log_bytes"]) is not int
                    or not 0 <= receipt["log_bytes"] <= registration["max_log_bytes"]
                    or not isinstance(receipt["completed_at"], str)):
                raise ValueError("Malformed receipt")
            if ((receipt["outcome"] == "succeeded" and receipt["exit_code"] != 0)
                    or (receipt["outcome"] == "failed" and receipt["exit_code"] == 0)):
                raise ValueError("Outcome disagrees with exit code")
            return receipt, None
        except FileNotFoundError:
            return None, "No completion receipt"
        except (OSError, ValueError, KeyError, TypeError):
            return None, "Invalid or stale completion receipt"

    def inspect(self, job_id):
        job = self.job(job_id)
        receipt, reason = self._receipt(job)
        state = job["state"]
        if receipt:
            observed = receipt["outcome"]
        elif state == "registered":
            observed = "registered"
        else:
            try:
                held = lease_is_held(self.job_directory(job_id) / "wrapper.lock")
            except OSError:
                held = False
            observed = "running" if held and state == "running" and job["wrapper_claimed"] else "unknown"
        return {
            "job_id": job_id, "grant_id": job["grant_id"],
            "workspace_id": job["workspace_id"], "worker_thread": job["registration"]["worker_thread"],
            "recorded_state": state, "observed_state": observed,
            "wrapper_pid": job["wrapper_pid"], "command_pid": job["command_pid"],
            "receipt": receipt, "reason": None if receipt or observed == "registered" else reason,
            "log_path": str(self.job_directory(job_id) / "output.log"),
        }

    def recover(self, job_id):
        """Reconcile saved evidence only; never launch or signal a saved PID."""
        result = self.inspect(job_id)
        if result["observed_state"] in TERMINAL | {"unknown"}:
            with self.connect(write=True) as connection:
                # Recheck under the write lock so an arriving receipt wins.
                result = self.inspect(job_id)
                if result["observed_state"] in TERMINAL | {"unknown"}:
                    # Lost evidence must not reactivate an old reservation over a successor.
                    if not (result["recorded_state"] in TERMINAL and result["observed_state"] == "unknown"):
                        connection.execute("UPDATE jobs SET state = ? WHERE id = ?",
                                           (result["observed_state"], identity(job_id)))
        return self.inspect(job_id)
