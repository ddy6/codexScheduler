# Standalone timer

`scripts/timer.py` uses only Python's standard library on macOS and Linux. Copy it
alone if all you need is a timer, or install the complete skill for team guidance.
It does not launch or poll jobs, contact agents, install services, enforce a task
budget, or grant continuation. All identifiers below are synthetic.

Clock binding reads `/proc/sys/kernel/random/boot_id` on Linux or invokes
`/usr/sbin/sysctl -n kern.boottime` on macOS. Some agent sandboxes block those
reads. If access is denied, use the host's supported timer or manual polling;
this script does not bypass the restriction or weaken its clock check.

## Anchor once, then wait

Resolve the script relative to the installed skill. From a checkout:

```sh
python3 skills/poll-review/scripts/timer.py start \
  --record state/poll-cycle-1 --seconds 1800 \
  --task example-check --generation 1 --cycle 1 \
  --root example-engineer --helper example-reviewer \
  --job-kind exec-session --job-handle example-handle
python3 skills/poll-review/scripts/timer.py wait --record state/poll-cycle-1
```

The helper anchors the interval on receiving the handoff, before review. `start`
creates an exclusive private directory and immutable `timer.json` with the binding,
monotonic deadline and clock anchor. Keep one record directory per cycle; duplicate
starts fail without resetting the deadline. Reusing a handoff with a different path
is still a duplicate, not a new allowance. The host/controller enforces that rule.

`wait` sleeps until the original deadline, including time already spent reviewing.
It reserves `wait.claim` before sleeping and writes `outcome.json` when done. Each
record permits one waiter, even if that waiter crashes. The longest sleep chunk is
60 seconds; `--chunk-seconds` can shorten cancellation latency. Waiting uses no
model or network calls. Use your host's supported long-tool completion mechanism;
an asynchronous tool handle is not automatically a future agent wake. Local yield
rules still apply. If no automatic return exists, the user must poll.

The due outcome contains the exact binding and observed lateness. It means only
that the deadline passed. The root must validate the binding and inspect its owned
job handle before interpreting success or starting further work. The host's
separate delivery record must reserve allowance and send intent before any wake.

## Review before the margin

For already assigned work, the engineer supplies private frozen UTF-8 snapshots
and their expected SHA-256 hashes. Do not review changing files. While a deadline
is pending, read each assigned snapshot through:

```sh
python3 skills/poll-review/scripts/timer.py read-review \
  --record state/poll-cycle-1 \
  --snapshot state/review-batch/change.source \
  --sha256 ENGINEER_SUPPLIED_SHA256
```

The command checks time before and after the read, limits one read to 32 KiB, and
verifies the hash. It returns source only when `review_allowed` is true. The skill
also bounds the entire batch to 32 KiB by default; the script does not track batch
or model context size. No content is returned if the margin is reached during the
read. Stop review and proceed to waiting/due handling when permission is false.
Never pair a clock check with an unconditional source read.

Check `status` before and after short comparison/reasoning steps too. The default
review margin is 10 seconds; `start --margin` accepts 0–60 seconds. Choose a margin
that leaves enough time for your host's tool latency. The guard controls source
reads, not model preemption; a long inference/tool step can still delay the return.

## Inspect or cancel

```sh
python3 skills/poll-review/scripts/timer.py status --record state/poll-cycle-1
python3 skills/poll-review/scripts/timer.py cancel --record state/poll-cycle-1
```

`status` performs no job or process inspection and does not modify the deadline.
`wait_claimed` means an attempt was reserved, not that a waiter is alive. `due`
means the clock passed the deadline, not that a waiter finished or a message was
sent. Inspect the saved outcome when needed; missing/partial evidence remains
unresolved. Never delete a claim to recover or rerun a job to learn its state.

`cancel` creates an idempotent stop marker. Status and guarded reads then refuse
review/due eligibility; a waiter notices it by the next chunk. Ctrl-C to the waiter
also writes the stop marker. Cancellation affects this timer only, not the job or
an already sent message. Revoking continuation requires the host's stop mechanism
as well. A saved due outcome must not be consumed after revocation.

Exit code 0 returns a record, not success of the underlying job. Invalid inputs,
duplicate starts/waits, hash mismatches, oversized snapshots and uncertain clock
continuity return code 2 with a diagnostic. `start` accepts 0.001 seconds through
seven days as a timer hang bound, not a task-authority expiry.

## Recovery limits

Records are trusted local files, not a tamper-resistant permission store. Keep
them private and never edit deadlines or identifiers. A boot identity binds the
clock to the current macOS/Linux boot. A monotonic regression, changed boot, or
wall/monotonic elapsed-time disagreement above two seconds stops automatic use.
Clock corrections and some suspend/resume behavior can therefore require manual
reconciliation. Do not restart the interval or assume an old due event is eligible.

A killed process, partial write or missing record does not authorize replay. The
timer does not recover across reboot or provide exactly-once external delivery.
At a quiet boundary, reconcile the host's pending cycle, consumed allowance and
job evidence before assigning any successor. Use the durable scheduler when you
need its job receipts, controller reservations and delivery reconciliation.
