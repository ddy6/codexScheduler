# Bounded completion delivery

Completion delivery connects the local job runner to a dedicated Codex worker.
It waits in ordinary code, sends one event after verifying the job receipt, and
records the resulting turn. Existing grants have no continuation allowance until
you explicitly configure one.

## Verified result

The integrated trial passed on October 26, 2026 (UTC), using local macOS,
Python 3.14.3, Codex CLI 0.164.0, and websockets 17.3. The worker used the configured
GPT-6.1 Sol model.

The real registry and detached wrapper ran one harmless command. Its launching
worker turn ended before the command was released to finish. A matching receipt
then authorized one dispatcher reservation and one turn-start request. The worker
recalled context from its initial turn, and reconciliation matched the saved
event input to the acknowledged turn. It finished at idle with two turns total.

The grant consumed one job attempt, one continuation, and two of its ten status
checks. Read-only permissions, disabled tool network access, and on-request user
approvals were retained. Dispatch through final verification took about 3.4
seconds in this small trial; this is not a latency benchmark. The temporary server
was closed afterward, with no installed service or running trial left behind.

All 75 offline checks pass. Revocation, lost acknowledgements, crashes around
dispatch, exhausted budgets, and unavailable approvals were tested offline; this
live trial covered the normal path. Actual reboot, extended idle periods, native
child agents, and competing interactive writers remain untested. Runtime evidence
stays local; this summary omits prompts, machine paths, and session identifiers.

The worker must belong exclusively to this controller. Do not target your live
user conversation or share the worker with another writer. The controller checks
for an idle worker and empty input queue before dispatch, but the API does not
provide an atomic exclusion against another interactive client. Local event
leases and registry reservations serialize cooperating controllers.

## Configure one bounded task

Install the optional Codex transport dependency in your local environment:

```sh
.venv/bin/python -m pip install -r requirements-prototype.txt
```

Start with a grant and registered job from the [runner guide](job-runner.md).
The exact worker must already exist on a local Codex app-server Unix socket.
Worker creation and service installation are not part of these commands.

For a registry created by the earlier runner, first add the delivery tables:

```sh
python3 -m codex_scheduler upgrade
```

Upgrade preserves existing jobs and consumed launch attempts. It does not enable
continuations. Record an explicit continuation allowance on the task grant:

```sh
python3 -m codex_scheduler configure-delivery --grant GRANT_ID \
  --socket /path/to/worker.sock --max-continuations 1 --max-checks 120
```

The default expected permissions are read-only, network access disabled,
on-request approvals, and user review. For a worker with other approved settings,
provide its exact permission snapshot with `--permissions-file PATH`. The JSON
keys are `sandbox`, `approvalPolicy`, `approvalsReviewer`, and optionally
`activePermissionProfile`. Store the file in a private location such as `state/`.
Delivery compares the current settings to the snapshot and never overrides them.

Save the substantive continuation while the engineer still has task context,
before launching and yielding:

```sh
python3 -m codex_scheduler arm --job JOB_ID \
  --instructions "Read the receipt, report the verification result, and stop."
python3 -m codex_scheduler launch --job JOB_ID
python3 -m codex_scheduler watch --job JOB_ID --interval 2
```

`watch` checks local records while the job runs; it makes no model calls to ask
for progress. Once the receipt is verified and the worker is idle, it reserves an
attempt and starts one turn in the exact worker thread. The event contains its
stable ID, job identity, receipt path, outcome, task scope, and saved instructions.
Command output and full logs are never copied into the prompt.

The controller uses the app-server's thread/turn API over the same Unix WebSocket
transport tested by the worker experiment. It starts turns directly so it can
record the acknowledged turn ID. The earlier experiment used the CLI queue.

## Bounds and stopping

Each job has at most one event and one dispatch attempt. Continuation and check
allowances belong to the task grant and persist across new jobs and controller
restarts. Configuration cannot be overwritten to reset them. A check is one
controller tick, including a bounded worker-history scan when needed. Each scan
reads at most five pages of twenty turns; each RPC also has finite time and
message limits. Local `delivery-status` and `delivery-policy` reads do not consume
checks or contact Codex.

One armed or unresolved event reserves its worker and workspace until it closes.
This first controller handles one job at a time; it does not advance a queue or
allow a worker to arm a successor while its current continuation is open.

Inspect local state with:

```sh
python3 -m codex_scheduler delivery-status --job JOB_ID
python3 -m codex_scheduler delivery-policy --grant GRANT_ID
```

| State | Meaning |
| --- | --- |
| `pending` | Waiting for a verified receipt or idle worker; no send reserved |
| `dispatching` | Attempt committed before sending; a crash requires reconciliation |
| `started` | Worker accepted a turn, or history confirms it is running |
| `completed` | Matching history confirms completion and the worker is idle |
| `failed` | The matching worker turn failed or was interrupted |
| `uncertain` | Delivery outcome is unknown; do not resend |
| `blocked` | Operator input, changed permissions, or inconsistent evidence requires review |
| `revoked` | Future delivery was stopped before sending |

These are controller outcomes, not a verdict on the engineer's work. A completed
turn can contain a request for a decision or a handoff. Read its result before
authorizing more work. Check-budget exhaustion ends automation and leaves the
reservation intact for operator review; there is no automatic budget refill.

Stop future delivery with:

```sh
python3 -m codex_scheduler revoke-delivery --grant GRANT_ID
```

Revocation blocks reservations made afterward. A reserved send or active turn may
still finish; an already consumed attempt is never restored. Revoking delivery
does not kill the command, signal saved PIDs, or interrupt an active worker turn.
The controller does not approve tool actions or answer user questions. An
approval request stops it for the operator.

## Interrupted or uncertain delivery

The registry commits the event text and consumed allowance before the send. A
transport error or a crash around dispatch can leave the outcome uncertain:

```sh
python3 -m codex_scheduler reconcile-delivery --job JOB_ID
```

Reconciliation never starts a turn. It searches bounded worker history for the
exact saved event input and checks the acknowledged turn ID when one exists.
Missing, duplicated, truncated, or mixed-input evidence stays unresolved. Neither
absence from a scan nor an unacknowledged send authorizes a second prompt. Starting
`watch` again on a reserved event also reconciles it rather than resending.

Keep one authoritative registry on the same host as the worker. Runtime records
contain private paths, thread identifiers, and instructions. They belong in an
ignored location, not in commits or shared examples.

## Synthetic integration check

The bounded harness starts its own read-only worker and uses the actual runner,
registry, and dispatcher for one harmless job and one continuation:

```sh
.venv/bin/python -m prototypes.delivery_trial --run-dir runs/delivery-trial-001 --preflight-only
.venv/bin/python -m prototypes.delivery_trial --run-dir runs/delivery-trial-001 --retry-bootstrap
```

The first command is a transport check without model work. The second starts
model work and must be within an approved trial. Existing worker trials and their
consumed allowances are not reused. The recorded trial's allowance is fully
consumed; it must not be rerun. Choose a new directory only for a newly authorized
trial, not to retry an event whose outcome is uncertain.

Interface: [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).
The command and WebSocket APIs are experimental; this is a local integration,
not a production service or a guarantee across Codex versions.
