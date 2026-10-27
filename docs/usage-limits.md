# Usage limits and interrupted workers

A worker can become idle after exhausting usage without finishing its task.
Progress capture records a confirmed interruption as `task_state: blocked_on_usage`.
Idle notifications, quota refill, and capture restarts do not clear it. A new
observed turn or a confirmed terminal result can change it. Read the handoff and
job evidence before treating the engineering task as finished.

## Read usage

```sh
codex-scheduler --project scheduler.toml usage-status --grant GRANT_ID
```

This uses the grant's configured server to read percentages, window durations,
reset times, and observation time. It invokes no model and consumes no delivery
checks. Reset times are Unix seconds. Check the window duration; `primary` is not
assumed to mean five hours. Unknown or unsupported data stays unknown, and elapsed
reset time never proves refill. All reported buckets are considered conservatively.
Oversized quota responses are reported as [receive failures](response-limits.md),
not converted into unknown quota.

Progress files include an initial read and subsequent account-limit notifications.
Sparse notifications preserve other buckets and omitted windows. Values are last
observed, not a live availability guarantee. At attachment the observer also reads
the latest turn's identity, status, and error classification to detect an earlier
usage failure. It omits raw backend messages, account details, and reset credits.

## Authorize recovery

Recovery defaults to zero. Schema 3 adds durable waits and resume records. Upgrade
an older registry explicitly; this preserves counters and existing policies:

```sh
codex-scheduler --project scheduler.toml upgrade
```

For a newly authorized task with one normal continuation and at most one recovery,
set these profile ceilings:

```toml
[limits]
max_continuations = 2
max_usage_resumes = 1
max_checks = 120
```

Configure the matching allowance using the actual binding and approved permissions:

```sh
codex-scheduler --project scheduler.toml configure-delivery --grant GRANT_ID \
  --socket /path/to/worker.sock --max-continuations 2 --max-usage-resumes 1
```

Supply `--permissions-file` when the snapshot differs from the documented defaults.
Register, arm, and use `run` as in the setup guide. `watch` supervises an existing
job without relaunching it. Bounds are immutable; do not replace a grant to evade
consumed allowance. Old policies do not gain recovery authority through upgrade.

Recovery covers confirmed usage failures in **scheduler-owned continuation turns**.
It does not guess what an unrelated worker was doing or automatically resume
engineering interrupted before a scheduled continuation. Those interruptions are
reported for the supervisor's handoff within the existing authorization.

## Waiting and effects

The unresolved event retains its workspace reservation. `delivery-status` shows
`state: blocked`, `reason: blocked_on_usage`, and a `usage` record containing the
interrupted turn and quota snapshot. `delivery-policy` includes the consumed
usage-resume count alongside total continuations and checks.

When allowance remains, the waiter sleeps in ordinary code until the reported
reset. Sleeping spends no model turns or delivery checks. Local revocation and
controller state are checked about once per second. Before sending, it requires
fresh available quota, the same latest failed turn, an idle worker, an empty queue,
unchanged permissions, and the matching job receipt.

Each resume persists a unique request and consumes both one recovery attempt and
one total continuation before sending. Its instructions retain the original scope
and receipt and tell the worker to inspect existing work before continuing. The
job launch stays consumed. Successor jobs and restarts do not refill allowance.

Unknown sends are reconciled, never replayed. The original event retains its
original turn ID; `usage.resume` identifies the recovery turn. Revocation prevents
later reservations, while already reserved attempts remain consumed.

If quota remains exhausted, missing, unknown, or past reset metadata causes a
handoff. Scheduled waiting has a
24-hour hang guard per interruption and a monotonic bound per wait. Exhausted
checks or attempts, changed bindings, queued input, and operator requests stop
automation. Weekly limits and spend controls do not cause unbounded retries.

The supervisor should report blocked work separately from completion. Once usage
is available, continue the same worker only within approved scope and allowance;
inspect saved work and receipts before repeating effects. A reset grants no new
permission. Observation uses no inference; an authorized resume starts a model
turn and uses normal model usage.

Recovery is tested with synthetic workers. Live exhaustion/reset recovery remains
unverified. Protocol: [OpenAI app-server documentation](https://learn.chatgpt.com/docs/app-server#auth-endpoints).
