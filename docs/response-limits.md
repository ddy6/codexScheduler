# Response bounds and completion confirmation

Codex responses have an 8 MiB receive limit by default, including for older policies
that have no stored transport setting. New policies save `max_receive_bytes` when
configured. Limits are integer bytes, from 1 KiB to 64 MiB; disabling the bound is
not supported.

For an existing task, an invocation can override only the transport setting:

```sh
codex-scheduler --project scheduler.toml --max-receive-bytes 16777216 \
  reconcile-delivery --job JOB_ID
```

Global options go before the subcommand. The override does not rewrite the saved
policy, refill checks, authorize another turn, or change permissions. With a
project profile, its ceiling must allow that value:

```toml
[limits]
max_receive_bytes = 16777216
```

Changing a profile does not amend existing policies. A newly configured policy
uses the profile's selected receive bound; an existing policy keeps its setting
unless an invocation explicitly overrides it.

## Reconcile the saved turn

With an acknowledged turn ID, confirmation verifies that exact turn's saved user
input and status. Status pages omit items. Full item reads are restricted to the
acknowledged turn, so unrelated command output and long replies are not fetched.
Additional user input, mismatched text, missing items, and truncated evidence
remain unresolved. A large individual item can still exceed the receive cap.

Without an acknowledgement, discovery searches bounded status metadata and initial
inputs. A matching candidate must pass full scoped input verification. Duplicate
matches or an incomplete search never authorize a resend. Unsupported scoped
history APIs cause a handoff; there is no fallback to unrelated full history.

Each check permits at most 32 history requests, five status pages of twenty turns,
and five item pages per candidate. A 15-second budget is checked before each
history request; an in-flight RPC retains its own finite timeout. Reconciliation
consumes one existing controller check per tick, not one per RPC. It consumes no
new job, continuation, or usage-recovery attempt.

## Oversized responses

Delivery records preserve an explicit `response_too_large` reason, receive bound,
RPC method, and failure phase:

| Phase | Meaning |
| --- | --- |
| `worker_verification` | Verification failed before the next completion or recovery request was reserved; delivery is blocked |
| `dispatch_acknowledgement` | A reserved send may have happened, but its reply could not be read; delivery is uncertain |
| `completion_confirmation` | A sent turn cannot be confirmed; delivery is uncertain and its saved acknowledgement is retained |

An oversized confirmation does not mean the worker failed or that a successful
job needs rerunning. Inspect saved evidence, adjust the transport bound if needed,
and reconcile the same event. Never create a replacement job or resend because
confirmation failed. Checks and consumed attempts remain intact.

Progress capture reports `observer_state: response_too_large` with a structured
`failure` record. It does not claim completion. Quota reads propagate this failure
instead of treating it as missing quota data. No automatic cap increase, reconnect,
or model call explains the failure.
