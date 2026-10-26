"""Durable completion events. Unknown sends are reconciled, never replayed."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .registry import SchedulerError, TERMINAL, identity, nonempty
from .storage import canonical, exclusive_lease, utc_now


OPEN = {"pending", "dispatching", "uncertain", "started", "blocked"}
DONE = {"completed", "failed", "revoked"}
DEFAULT_PERMISSIONS = {
    "sandbox": {"type": "readOnly", "networkAccess": False},
    "approvalPolicy": "on-request", "approvalsReviewer": "user",
}


def fingerprint(receipt):
    return hashlib.sha256(canonical(receipt).encode()).hexdigest()


class DeliveryStore:
    def __init__(self, registry):
        self.registry = registry
        if registry.version < 2:
            raise SchedulerError("Upgrade this registry before configuring delivery")

    def configure(self, grant_id, *, socket_path, max_continuations=1, max_checks=120,
                  permissions=None):
        grant = self.registry.grant(grant_id)
        socket_path = Path(socket_path).resolve()
        if type(max_continuations) is not int or not 1 <= max_continuations <= 1000:
            raise SchedulerError("max_continuations must be between 1 and 1000")
        if type(max_checks) is not int or not 1 <= max_checks <= 10000:
            raise SchedulerError("max_checks must be between 1 and 10000")
        permissions = DEFAULT_PERMISSIONS if permissions is None else permissions
        if (not isinstance(permissions, dict) or not isinstance(permissions.get("sandbox"), dict)
                or not {"approvalPolicy", "approvalsReviewer"} <= permissions.keys()
                or set(permissions) - {"sandbox", "approvalPolicy", "approvalsReviewer", "activePermissionProfile"}
                or len(canonical(permissions)) > 16384):
            raise SchedulerError("Expected an explicit sandbox and approval snapshot")
        spec = {
            "socket_path": str(socket_path), "max_continuations": max_continuations,
            "max_checks": max_checks, "permissions": permissions, "created_at": utc_now(),
            "worker_thread": grant["worker_thread"], "workspace": grant["workspace"],
        }
        try:
            with self.registry.connect(write=True) as connection:
                connection.execute("INSERT INTO continuation_grants(grant_id, spec) VALUES (?, ?)",
                                   (grant_id, canonical(spec)))
        except sqlite3.IntegrityError as error:
            raise SchedulerError("Delivery bounds already exist and cannot be reset") from error
        return self.policy(grant_id)

    def policy(self, grant_id):
        with self.registry.connect() as connection:
            row = connection.execute("SELECT * FROM continuation_grants WHERE grant_id = ?",
                                     (identity(grant_id),)).fetchone()
        if row is None:
            raise SchedulerError("Grant has no automatic-continuation allowance")
        return {**dict(row), "spec": json.loads(row["spec"])}

    def arm(self, job_id, instructions):
        nonempty(instructions, "Continuation instructions")
        job = self.registry.job(job_id)
        registration = job["registration"]
        if self.registry.grant(job["grant_id"])["worker_thread"] != registration["worker_thread"]:
            raise SchedulerError("Worker identity changed")
        policy = self.policy(job["grant_id"])
        try:
            with self.registry.connect(write=True) as connection:
                current = connection.execute("SELECT * FROM continuation_grants WHERE grant_id = ?",
                                             (job["grant_id"],)).fetchone()
                if (current["revoked_at"]
                        or current["consumed_continuations"] >= policy["spec"]["max_continuations"]
                        or current["consumed_checks"] >= policy["spec"]["max_checks"]):
                    raise SchedulerError("Continuation grant revoked or exhausted")
                connection.execute(
                    "INSERT INTO deliveries(event_id, job_id, grant_id, workspace_id, worker_thread, instructions, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (uuid.uuid4().hex, job_id, job["grant_id"], job["workspace_id"],
                     registration["worker_thread"], instructions, utc_now()),
                )
        except sqlite3.IntegrityError as error:
            raise SchedulerError("Job, worker, or workspace already has a continuation") from error
        return self.event(job_id)

    def event(self, job_id):
        with self.registry.connect() as connection:
            row = connection.execute("SELECT * FROM deliveries WHERE job_id = ?", (identity(job_id),)).fetchone()
        if row is None:
            raise SchedulerError("Job is not armed for continuation")
        return dict(row)

    def update(self, job_id, *, state, reason=None, turn_id=None):
        with self.registry.connect(write=True) as connection:
            connection.execute(
                "UPDATE deliveries SET state = ?, reason = ?, turn_id = COALESCE(?, turn_id) WHERE job_id = ? "
                "AND state NOT IN ('completed','failed','revoked')",
                (state, reason, turn_id, identity(job_id)),
            )
        return self.event(job_id)

    def consume_check(self, job_id):
        event = self.event(job_id)
        with self.registry.connect(write=True) as connection:
            row = connection.execute("SELECT * FROM continuation_grants WHERE grant_id = ?", (event["grant_id"],)).fetchone()
            if row["consumed_checks"] >= json.loads(row["spec"])["max_checks"]:
                raise SchedulerError("Status-check allowance exhausted; operator handoff required")
            connection.execute("UPDATE continuation_grants SET consumed_checks = consumed_checks + 1 WHERE grant_id = ?",
                               (event["grant_id"],))

    def reserve(self, job_id):
        with self.registry.connect(write=True) as connection:
            event = self.event(job_id)
            policy = self.policy(event["grant_id"])
            result = self.registry.inspect(job_id)
            if event["state"] != "pending" or event["attempts"]:
                raise SchedulerError("Completion has already been reserved; reconcile it")
            if policy["revoked_at"] or policy["consumed_continuations"] >= policy["spec"]["max_continuations"]:
                raise SchedulerError("Continuation grant revoked or exhausted")
            if result["observed_state"] not in TERMINAL or not result["receipt"]:
                raise SchedulerError("Completion is not verified")
            receipt_path = self.registry.job_directory(job_id) / "completion.json"
            text = canonical({
                "event": "job_completed", "event_id": event["event_id"], "job_id": job_id,
                "grant_id": event["grant_id"], "outcome": result["observed_state"],
                "exit_code": result["receipt"]["exit_code"], "receipt_path": str(receipt_path),
                "scope": self.registry.grant(event["grant_id"])["scope"],
                "instructions": event["instructions"],
                "boundary": "Continue only the approved slice; stop for new scope, approval, or operator decisions.",
            })
            connection.execute("UPDATE continuation_grants SET consumed_continuations = consumed_continuations + 1 WHERE grant_id = ?",
                               (event["grant_id"],))
            connection.execute(
                "UPDATE deliveries SET state = 'dispatching', attempts = 1, request_text = ?, receipt_hash = ? WHERE job_id = ?",
                (text, fingerprint(result["receipt"]), job_id),
            )
        return self.event(job_id)

    def revoke(self, grant_id):
        self.policy(grant_id)
        with self.registry.connect(write=True) as connection:
            connection.execute("UPDATE continuation_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE grant_id = ?",
                               (utc_now(), grant_id))
            connection.execute(
                "UPDATE deliveries SET state = 'revoked', reason = 'Future continuation revoked' "
                "WHERE grant_id = ? AND attempts = 0 AND state IN ('pending','blocked')", (grant_id,),
            )
        return self.policy(grant_id)


class NeedsOperator(SchedulerError):
    pass


class Dispatcher:
    def __init__(self, registry, adapter_factory):
        self.store = DeliveryStore(registry)
        self.registry = registry
        self.adapter_factory = adapter_factory

    def step(self, job_id, *, reconcile_only=False):
        event = self.store.event(job_id)
        if event["state"] in DONE:
            return event
        lease = self.registry.root / ("delivery-" + event["event_id"] + ".lock")
        try:
            with exclusive_lease(lease):
                return self._step(job_id, reconcile_only)
        except BlockingIOError as error:
            raise SchedulerError("Another controller owns this completion event") from error

    def _step(self, job_id, reconcile_only):
        event = self.store.event(job_id)
        if event["state"] in DONE:
            return event
        if event["state"] == "blocked" and not event["attempts"]:
            return event
        self.store.consume_check(job_id)
        event = self.store.event(job_id)
        policy = self.store.policy(event["grant_id"])
        result = self.registry.inspect(job_id)
        if event["state"] == "pending" and policy["revoked_at"]:
            return self.store.update(job_id, state="revoked", reason="Future continuation revoked")
        if result["observed_state"] in {"registered", "running"}:
            return event
        if result["recorded_state"] == "launching" and not result["receipt"]:
            # Wrapper startup can lag the launching CLI. Bounded checks wait, never relaunch.
            return event
        if not result["receipt"] or result["observed_state"] not in TERMINAL:
            return self.store.update(job_id, state="blocked", reason="Completion evidence is unknown")
        if event["attempts"] and fingerprint(result["receipt"]) != event["receipt_hash"]:
            return self.store.update(job_id, state="blocked", reason="Completion receipt changed after reservation")
        try:
            with self.adapter_factory(policy["spec"]) as adapter:
                snapshot = adapter.snapshot(event["worker_thread"])
                if (snapshot["thread"]["id"] != event["worker_thread"]
                        or Path(snapshot["cwd"]).resolve() != Path(policy["spec"]["workspace"]).resolve()
                        or any(snapshot.get(key) != value for key, value in policy["spec"]["permissions"].items())
                        or snapshot["thread"].get("canAcceptDirectInput") is False):
                    raise NeedsOperator("Worker identity, workspace, or permissions changed")
                if event["attempts"]:
                    return self._reconcile(adapter, event, snapshot)
                if reconcile_only:
                    return event
                if snapshot["thread"]["status"]["type"] != "idle":
                    return self.store.update(job_id, state="pending", reason="Worker is still active")
                if adapter.has_queued_input(event["worker_thread"]):
                    raise NeedsOperator("Worker has other queued input")
                event = self.store.reserve(job_id)
                # Reservation is the revocation boundary. Recheck to catch a stop already recorded.
                if self.store.policy(event["grant_id"])["revoked_at"]:
                    return self.store.update(job_id, state="revoked", reason="Revoked before send; allowance remains consumed")
                turn_id = adapter.start(event)
                nonempty(turn_id, "Acknowledged turn ID", 256)
                return self.store.update(job_id, state="started", turn_id=turn_id)
        except NeedsOperator as error:
            return self.store.update(job_id, state="blocked", reason=str(error))
        except Exception:
            current = self.store.event(job_id)
            if current["attempts"]:
                return self.store.update(job_id, state="uncertain", reason="Transport outcome unknown; reconcile without resending")
            return self.store.update(job_id, state="blocked", reason="Worker transport unavailable before dispatch")

    def _reconcile(self, adapter, event, snapshot):
        matches = adapter.find_event(event)
        if len(matches) != 1:
            return self.store.update(event["job_id"], state="uncertain", reason="No unique matching turn in bounded history")
        turn = matches[0]
        if event["turn_id"] and event["turn_id"] != turn["id"]:
            raise NeedsOperator("Acknowledged turn disagrees with saved event history")
        if turn["status"] in {"failed", "interrupted"}:
            return self.store.update(event["job_id"], state="failed", reason="Worker turn failed or was interrupted", turn_id=turn["id"])
        if turn["status"] == "completed":
            if snapshot["thread"]["status"]["type"] != "idle":
                raise NeedsOperator("Another worker turn is active after this completion")
            return self.store.update(event["job_id"], state="completed", turn_id=turn["id"])
        flags = snapshot["thread"]["status"].get("activeFlags", [])
        if any(flag in {"waitingOnApproval", "waitingOnUserInput"} for flag in flags):
            raise NeedsOperator("Worker requires operator input")
        return self.store.update(event["job_id"], state="started", turn_id=turn["id"])

    def watch(self, job_id, *, interval=2):
        if not 0.05 <= interval <= 60:
            raise SchedulerError("Polling interval must be between 0.05 and 60 seconds")
        while True:
            result = self.step(job_id)
            if result["state"] not in {"pending", "started", "dispatching"}:
                return result
            time.sleep(interval)
