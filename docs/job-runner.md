# Local job runner

The runner registers and executes bounded local commands. It records a task grant,
reserves an attempt before starting a wrapper, and saves a completion receipt.
It does not call Codex or continue a worker thread yet.

Run from the source checkout with Python 3.11 or newer on a POSIX host. The current
checks run on macOS; Windows support and package installation come later. The core
uses the Python standard library. Only the separate worker experiment needs the
WebSocket dependency.

## Try a harmless job

Create a grant for one command in the current workspace:

```sh
python3 -m codex_scheduler create-grant --workspace . --thread WORKER_THREAD_ID \
  --scope "Run one offline verification" --max-jobs 1 --timeout 60 \
  --max-log-bytes 65536
```

The thread field records the exact intended engineering worker; the runner does
not contact it. Replace `GRANT_ID` below with the returned `grant_id`:

```sh
python3 -m codex_scheduler register --grant GRANT_ID -- python3 -c 'print(sum(range(10)))'
```

Registration saves the command and its bounds without running it. Replace `JOB_ID`
with the returned `job_id`, then launch:

```sh
python3 -m codex_scheduler launch --job JOB_ID
```

Launch returns immediately. The detached wrapper owns the command and writes its
receipt even after the launching CLI exits. Check the result later:

```sh
python3 -m codex_scheduler status --job JOB_ID
python3 -m codex_scheduler grant-status --grant GRANT_ID
```

Commands are argv lists, without a shell. Run foreground commands: backgrounding
work inside a command is outside this runner's contract. Commands inherit the
invoker's OS permissions and environment. Recording a grant does not create a
sandbox, grant credentials, or authorize work beyond the task you approved.

## Status and recovery

| Observed state | Meaning |
| --- | --- |
| `registered` | Saved, with no launch attempt yet |
| `running` | The claimed wrapper still holds its job lease |
| `succeeded` | A matching receipt records exit code zero |
| `failed` | A matching receipt records a command or executable failure |
| `timed_out` | The wrapper enforced the runtime limit |
| `output_limit` | The command exceeded its log allowance |
| `unknown` | Evidence is missing, invalid, stale, or the wrapper was lost |

Status reports both the recorded state and the currently observed state. It opens
the registry read-only and returns a log path, never the full log. Saved PIDs are
diagnostic only. A private launch token, registered wrapper claim, and OS file lock
identify the cooperating wrapper; PID reuse cannot establish success or authorize
signaling a process.

After restarting a controller, reconcile saved evidence with:

```sh
python3 -m codex_scheduler recover --job JOB_ID
```

Recovery never launches a command, retries an attempt, or signals a saved PID. A
valid receipt can finish the registry update if the old controller stopped after
the receipt was written. A lost wrapper without a valid receipt becomes unknown.
Unknown work keeps its workspace reservation until evidence can resolve it; there
is no force-unlock or relaunch command. If an old terminal job loses its receipt,
its observed state becomes unknown without displacing a newer workspace job.

Launch attempts remain consumed after failures and restarts. A second launch of
the same job is refused. One active or unresolved job is allowed per workspace
within a registry, including across grants. Explicitly creating a new grant is a
new authorization, not a recovery step.

## Local records and limits

Records live under `state/scheduler/` by default. Set a different existing or new
location with `--state-dir PATH` before the subcommand. Grant creation initializes
the registry; inspection never creates a missing one. Keep one authoritative
registry per host and workspace. Separate copies cannot share counters or leases.
Do not put the registry on a network filesystem or copy it to reset allowance.

SQLite commits the launch reservation before process creation. The wrapper writes
and syncs an atomic receipt before updating terminal state. Command logs stop at
the granted byte limit; runtime and output violations terminate the process group
created by that live wrapper. If the wrapper itself crashes, child work may remain
alive: recovery reports uncertainty rather than killing a potentially reused PID.

Treat the registry, registration files, and receipts as trusted local state. They
can contain commands, paths, thread identifiers, and output. The source checkout
ignores its default `state/` directory; keep custom locations private too.

Run the offline checks with:

```sh
python3 -m unittest discover -s tests -v
```

Checks cover detached execution after the launching CLI exits, controller reopen,
launch races, failed spawns, stale receipts, missing leases, runtime and log limits,
and receipt recovery. Actual machine reboot, extended unattended operation, and
delivery to Codex are separate integration work.
