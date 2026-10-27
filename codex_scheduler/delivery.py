"""Durable completion events. Unknown sends are reconciled, never replayed."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .registry import SchedulerError, TERMINAL, identity, nonempty
from .storage import canonical, exclusive_lease, utc_now
from .usage import read_quota
from .transport import DEFAULT_RECEIVE_BYTES, ResponseTooLarge, receive_limit


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
                  permissions=None, max_usage_resumes=0, max_receive_bytes=DEFAULT_RECEIVE_BYTES):
        grant = self.registry.grant(grant_id)
        receive_limit(max_receive_bytes)
        socket_path = Path(socket_path).resolve()
        if type(max_continuations) is not int or not 1 <= max_continuations <= 1000:
            raise SchedulerError("max_continuations must be between 1 and 1000")
        if type(max_checks) is not int or not 1 <= max_checks <= 10000:
            raise SchedulerError("max_checks must be between 1 and 10000")
        if type(max_usage_resumes) is not int or not 0 <= max_usage_resumes <= 100:
            raise SchedulerError("max_usage_resumes must be between 0 and 100")
        if max_usage_resumes and self.registry.version < 3:
            raise SchedulerError("Upgrade this registry before authorizing usage recovery")
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
            "max_usage_resumes": max_usage_resumes,
            "max_receive_bytes": max_receive_bytes,
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
            used = (connection.execute("SELECT COUNT(*) FROM usage_resumes WHERE grant_id = ?", (grant_id,)).fetchone()[0]
                    if self.registry.version >= 3 else 0)
        if row is None:
            raise SchedulerError("Grant has no automatic-continuation allowance")
        return {**dict(row), "spec": json.loads(row["spec"]), "consumed_usage_resumes": used}

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
        result = dict(row)
        if self.registry.version >= 3:
            with self.registry.connect() as connection:
                wait = connection.execute("SELECT * FROM usage_waits WHERE job_id = ?", (job_id,)).fetchone()
                resume = connection.execute("SELECT * FROM usage_resumes WHERE job_id = ? ORDER BY sequence DESC LIMIT 1",
                                            (job_id,)).fetchone()
            if wait:
                result["usage"] = {**dict(wait), "quota": json.loads(wait["quota"]),
                                   "resume": dict(resume) if resume else None,
                                   "state": "blocked_on_usage" if row["state"] == "blocked" and row["reason"] == "blocked_on_usage"
                                   else row["state"]}
        return result

    def pause_for_usage(self, job_id, source_turn, quota):
        if self.registry.version < 3:
            return self.update(job_id, state="blocked", reason="blocked_on_usage; upgrade registry to preserve quota details")
        with self.registry.connect(write=True) as connection:
            existing = connection.execute("SELECT source_turn_id FROM usage_waits WHERE job_id = ?", (job_id,)).fetchone()
            if existing is None or existing["source_turn_id"] != source_turn:
                connection.execute("INSERT OR REPLACE INTO usage_waits VALUES (?, ?, ?, ?)",
                                   (job_id, source_turn, canonical(quota), time.time()))
            else:
                connection.execute("UPDATE usage_waits SET quota = ? WHERE job_id = ?", (canonical(quota), job_id))
            connection.execute("UPDATE deliveries SET state = 'blocked', reason = 'blocked_on_usage' WHERE job_id = ? "
                               "AND state NOT IN ('completed','failed','revoked')", (job_id,))
        return self.event(job_id)

    def reserve_usage_resume(self, job_id):
        with self.registry.connect(write=True) as connection:
            event = self.event(job_id)
            policy = self.policy(event["grant_id"])
            usage = event["usage"]
            if (event["state"] != "blocked" or event["reason"] != "blocked_on_usage" or policy["revoked_at"]
                    or policy["consumed_usage_resumes"] >= policy["spec"].get("max_usage_resumes", 0)
                    or policy["consumed_continuations"] >= policy["spec"]["max_continuations"]):
                raise SchedulerError("Usage recovery revoked, unavailable, or exhausted")
            result = self.registry.inspect(job_id)
            if (usage["quota"]["availability"] != "available" or not result["receipt"]
                    or fingerprint(result["receipt"]) != event["receipt_hash"]):
                raise SchedulerError("Usage recovery lacks available quota or unchanged completion evidence")
            resume_id = uuid.uuid4().hex
            payload = json.loads(event["request_text"])
            payload.update(event="resume_after_usage", event_id=resume_id, original_event_id=event["event_id"],
                           interrupted_turn=usage["source_turn_id"],
                           recovery="Inspect saved work and existing job receipts, then continue the unfinished approved slice. "
                                    "Do not relaunch the registered job or repeat completed effects. Stop at the existing boundary.")
            connection.execute("INSERT INTO usage_resumes(event_id,job_id,grant_id,source_turn_id,request_text,state) "
                               "VALUES (?,?,?,?,?,'dispatching')",
                               (resume_id, job_id, event["grant_id"], usage["source_turn_id"], canonical(payload)))
            connection.execute("UPDATE continuation_grants SET consumed_continuations = consumed_continuations + 1 WHERE grant_id = ?",
                               (event["grant_id"],))
            connection.execute("UPDATE deliveries SET state = 'dispatching', reason = 'Usage recovery reserved' WHERE job_id = ?", (job_id,))
        return self.event(job_id)

    def update_usage_resume(self, job_id, state, turn_id=None):
        with self.registry.connect(write=True) as connection:
            connection.execute("UPDATE usage_resumes SET state = ?, turn_id = COALESCE(?,turn_id) WHERE sequence = "
                               "(SELECT MAX(sequence) FROM usage_resumes WHERE job_id = ?)", (state, turn_id, job_id))

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
            if self.registry.version >= 3:
                connection.execute("UPDATE deliveries SET state = 'revoked', reason = 'Usage recovery revoked' "
                                   "WHERE grant_id = ? AND state = 'blocked' AND reason = 'blocked_on_usage' "
                                   "AND job_id IN (SELECT job_id FROM usage_waits)", (grant_id,))
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
        if event["state"] == "blocked":
            if event["reason"] == "blocked_on_usage" and "usage" in event:
                policy = self.store.policy(event["grant_id"])
                if policy["revoked_at"]:
                    return self.store.update(job_id, state="revoked", reason="Usage recovery revoked; attempts remain consumed")
                if not self._can_resume_usage(policy):
                    return event
            elif not event["attempts"]:
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
        usage_verification = event["state"] == "blocked" and event["reason"] == "blocked_on_usage"
        phase = "completion_confirmation" if event["attempts"] and not usage_verification else "worker_verification"
        try:
            with self.adapter_factory(policy["spec"]) as adapter:
                snapshot = adapter.snapshot(event["worker_thread"])
                if (snapshot["thread"]["id"] != event["worker_thread"]
                        or Path(snapshot["cwd"]).resolve() != Path(policy["spec"]["workspace"]).resolve()
                        or any(snapshot.get(key) != value for key, value in policy["spec"]["permissions"].items())
                        or snapshot["thread"].get("canAcceptDirectInput") is False):
                    raise NeedsOperator("Worker identity, workspace, or permissions changed")
                if event["attempts"]:
                    if event["state"] == "blocked" and event["reason"] == "blocked_on_usage":
                        if reconcile_only:
                            return event
                        return self._resume_usage(adapter, event, snapshot)
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
        except ResponseTooLarge as error:
            current = self.store.event(job_id)
            resume = current.get("usage", {}).get("resume")
            if resume and resume["state"] == "dispatching":
                self.store.update_usage_resume(job_id, "uncertain")
            if current["attempts"] and error.method == "turn/start":
                phase = "dispatch_acknowledgement"
            return self.store.update(job_id, state="blocked" if phase == "worker_verification" else "uncertain",
                                     reason=f"{phase} failed: {error}; no resend authorized")
        except NeedsOperator as error:
            return self.store.update(job_id, state="blocked", reason=str(error))
        except Exception:
            current = self.store.event(job_id)
            resume = current.get("usage", {}).get("resume")
            if resume and resume["state"] == "dispatching":
                self.store.update_usage_resume(job_id, "uncertain")
            if current["attempts"]:
                return self.store.update(job_id, state="uncertain", reason="Transport outcome unknown; reconcile without resending")
            return self.store.update(job_id, state="blocked", reason="Worker transport unavailable before dispatch")

    def _reconcile(self, adapter, event, snapshot):
        resume = event.get("usage", {}).get("resume")
        target = {**event, **resume} if resume else event
        matches = adapter.find_event(target)
        if len(matches) != 1:
            return self.store.update(event["job_id"], state="uncertain", reason="No unique matching turn in bounded history")
        turn = matches[0]
        if target["turn_id"] and target["turn_id"] != turn["id"]:
            raise NeedsOperator("Acknowledged turn disagrees with saved event history")
        if turn["status"] in {"failed", "interrupted"}:
            if resume:
                self.store.update_usage_resume(event["job_id"], "failed", turn["id"])
            if turn.get("error_code") == "UsageLimitExceeded":
                return self.store.pause_for_usage(event["job_id"], turn["id"], read_quota(adapter))
            return self.store.update(event["job_id"], state="failed", reason="Worker turn failed or was interrupted",
                                     turn_id=None if resume else turn["id"])
        if turn["status"] == "completed":
            if snapshot["thread"]["status"]["type"] != "idle":
                raise NeedsOperator("Another worker turn is active after this completion")
            if resume:
                self.store.update_usage_resume(event["job_id"], "completed", turn["id"])
            return self.store.update(event["job_id"], state="completed",
                                     turn_id=None if resume else turn["id"],
                                     reason="Completed through usage recovery; see saved resume" if resume else None)
        flags = snapshot["thread"]["status"].get("activeFlags", [])
        if any(flag in {"waitingOnApproval", "waitingOnUserInput"} for flag in flags):
            raise NeedsOperator("Worker requires operator input")
        if resume:
            self.store.update_usage_resume(event["job_id"], "started", turn["id"])
        return self.store.update(event["job_id"], state="started", turn_id=None if resume else turn["id"])

    @staticmethod
    def _can_resume_usage(policy):
        return (not policy["revoked_at"]
                and policy["consumed_usage_resumes"] < policy["spec"].get("max_usage_resumes", 0)
                and policy["consumed_continuations"] < policy["spec"]["max_continuations"])

    def _resume_usage(self, adapter, event, snapshot):
        if snapshot["thread"]["status"]["type"] != "idle":
            raise NeedsOperator("Worker changed after usage interruption")
        if set(snapshot["thread"]["status"].get("activeFlags", [])) & {"waitingOnApproval", "waitingOnUserInput"}:
            raise NeedsOperator("Worker requires operator input")
        if adapter.has_queued_input(event["worker_thread"]):
            raise NeedsOperator("Worker has other queued input")
        latest = adapter.latest_turn(event["worker_thread"])
        usage = event["usage"]
        if (not latest or latest["id"] != usage["source_turn_id"]
                or latest["status"] not in {"failed", "interrupted"}
                or latest.get("error_code") != "UsageLimitExceeded"):
            raise NeedsOperator("Usage interruption is no longer the worker's latest confirmed turn")
        quota = read_quota(adapter)
        self.store.pause_for_usage(event["job_id"], usage["source_turn_id"], quota)
        if quota["availability"] != "available":
            return self.store.event(event["job_id"])
        reserved = self.store.reserve_usage_resume(event["job_id"])
        resume = reserved["usage"]["resume"]
        if self.store.policy(event["grant_id"])["revoked_at"]:
            self.store.update_usage_resume(event["job_id"], "failed")
            return self.store.update(event["job_id"], state="revoked", reason="Revoked before usage resume; allowance remains consumed")
        turn_id = adapter.start({**event, **resume})
        nonempty(turn_id, "Acknowledged usage-resume turn ID", 256)
        self.store.update_usage_resume(event["job_id"], "started", turn_id)
        return self.store.update(event["job_id"], state="started", reason="Resumed after usage interruption")

    def watch(self, job_id, *, interval=2):
        if not 0.05 <= interval <= 60:
            raise SchedulerError("Polling interval must be between 0.05 and 60 seconds")
        event = self.store.event(job_id)
        lease = self.registry.root / ("watcher-" + event["event_id"] + ".lock")
        try:
            with exclusive_lease(lease):
                return self._watch(job_id, interval)
        except BlockingIOError as error:
            raise SchedulerError("Another waiter owns this completion event") from error

    def _watch(self, job_id, interval):
        while True:
            result = self.step(job_id)
            if result["state"] == "blocked" and result["reason"] == "blocked_on_usage":
                usage = result.get("usage")
                policy = self.store.policy(result["grant_id"])
                reset = usage["quota"]["reset_at"] if usage else None
                if (usage and usage["quota"]["availability"] == "available" and self._can_resume_usage(policy)
                        and policy["consumed_checks"] < policy["spec"]["max_checks"]):
                    continue
                # A reset time schedules a fresh check; it never proves refill.
                if (not usage or not self._can_resume_usage(policy)
                        or policy["consumed_checks"] >= policy["spec"]["max_checks"]
                        or reset is None or not time.time() < reset <= usage["created_at"] + 86400):
                    return result
                deadline = time.monotonic() + min(86400, reset - time.time())
                while time.time() < reset:
                    policy = self.store.policy(result["grant_id"])
                    if policy["revoked_at"]:
                        return self.store.update(job_id, state="revoked", reason="Usage recovery revoked; attempts remain consumed")
                    current = self.store.event(job_id)
                    if current["state"] != "blocked":
                        break
                    if (current["reason"] != "blocked_on_usage" or not self._can_resume_usage(policy)
                            or policy["consumed_checks"] >= policy["spec"]["max_checks"]
                            or time.monotonic() >= deadline):
                        return current
                    time.sleep(min(1, max(0, reset - time.time()), max(0, deadline - time.monotonic())))
                continue
            if result["state"] not in {"pending", "started", "dispatching"}:
                return result
            time.sleep(interval)
