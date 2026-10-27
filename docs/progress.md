# Automatic progress capture

Add `--progress` to an approved `run` or `watch` command:

```sh
codex-scheduler --project scheduler.toml run --job JOB_ID --progress
```

The observer uses a separate app-server connection and the grant's exact worker,
workspace, and permission binding. It resumes that binding without overrides,
then listens for existing events. It sends no prompt, approval response, or
supervisor message. Delivery continues through the existing bounded dispatcher.

Two rolling files appear under the registry's state directory:

```text
progress/GRANT_ID/progress.json
progress/GRANT_ID/progress.md
```

JSON is the authoritative snapshot; Markdown is a human-readable rendering.
Each file is replaced atomically with private permissions. They contain the
latest short worker message, up to twenty shared plan steps, tool activity,
and, when a job is selected, its receipt-backed outcome and delivery state.
They omit user inputs, reasoning, command arguments/output, and diffs. Worker
messages can still contain private data; keep the state directory ignored.
Copied text is evidence, never new instructions or authorization.

Oversized messages stop capture with `observer_state: response_too_large` and a
structured `failure` describing the method and byte bound. This is a capture
failure, not proof that the worker failed or finished. The global
`--max-receive-bytes` override also applies to the observer's connection.

Capture refreshes at most about once per second, plus startup and shutdown.
`updated_at` is the observer heartbeat; `last_worker_activity_at` changes only
when a supported worker event is observed. Neither proves the engineer is still
working. A completed worker turn does not establish that a job succeeded; the
job's verified exit code comes only from a matching receipt.

Quota percentages and reset metadata are read once at attachment and updated
from account notifications. The observer checks the latest turn's classification
once as well; it does not replay its messages. A confirmed usage interruption is
preserved as `task_state: blocked_on_usage` across idle updates and capture
restarts. Quota refill alone does not clear it or start a turn. See
[usage limits](usage-limits.md) for fresh reads and opt-in bounded recovery.

## Bounds and stopping

Capture defaults to 900 seconds or 10,000 received events, whichever comes first.
`--progress-duration` accepts 1–86,400 seconds; `--progress-max-events` accepts
1–100,000 events. RPC setup and connection shutdown also have bounded timeouts.
Large messages and plans are truncated. Storage holds one snapshot, not an
ever-growing transcript. Only one observer can write a grant's snapshot at once.

The attached observer stops when `run`/`watch` returns. Reaching a capture bound
or losing its connection stops observation, not an already authorized job or
continuation. After startup, capture failures do not alter delivery decisions.
If capture cannot attach initially, `run --progress` refuses before launch.

Revocation is checked locally about once per second. Operator-input requests
stop capture without a response. There is no automatic reconnect, history replay,
or model call to explain a gap. Restarting capture replaces the rolling snapshot,
preserves a recorded usage interruption, and assigns a new capture ID. It does not
reset any job or continuation allowance.
An abruptly killed observer may leave its last state marked `observing`; check
the timestamp. Its last worker state can be stale.

## Observe a longer engineering window

After configuring an existing grant's worker binding, run this in a separate
terminal or preserved foreground process:

```sh
codex-scheduler --project scheduler.toml observe-progress \
  --grant GRANT_ID --duration 1800 --max-events 20000
```

Add `--job JOB_ID` for one exact job; no job or worker is guessed. This command
does not launch work, reserve delivery, or consume continuation/check allowance.
It exits at its own bound or on disconnection, revocation, or operator input.
Use `run`/`watch` without `--progress` while this observer owns the snapshot.

The supervisor can read the snapshot on a user question, decision, or final
handoff. Routine copying uses no inference; a model reading or summarizing the
file still uses tokens. Displaying it automatically in a chat client is separate
from writing it locally.

Event shapes follow [OpenAI's app-server documentation](https://learn.chatgpt.com/docs/app-server#events).
Offline tests exercise capture and its CLI integration; live progress streaming
against a real worker has not yet been verified.
