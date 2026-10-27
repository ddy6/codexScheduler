# CLI reference

Use `codex-scheduler --help` or append `--help` to a subcommand for its flags.
`python -m codex_scheduler` is equivalent in the same environment.

Choose `--project PROFILE` or `--state-dir DIRECTORY` before the subcommand. They
are mutually exclusive. A profile supplies workspace paths and ceilings; without
one, records default to `state/scheduler/`. `--version` reports package metadata.

Global `--max-receive-bytes N` overrides the Codex transport bound for an invocation.
The default is 8 MiB, with a finite allowed range of 1 KiB–64 MiB. Project ceilings
apply to overrides. New delivery policies store their selected bound; overriding
an existing policy does not change its allowances. See [response limits](response-limits.md).

| Command | Purpose |
| --- | --- |
| `export-profile DESTINATION` | Copy the example TOML profile; refuse an existing file |
| `export-skill DESTINATION` | Copy the complete skill; refuse an existing directory |
| `project-info` | Inspect the selected profile without creating state |
| `create-grant --thread ID --scope TEXT` | Record explicit task authority and bounds |
| `grant-status --grant ID` | Read the grant and consumed job count |
| `register --grant ID --command NAME` | Save a profile's named argv command |
| `register --grant ID -- ARGV...` | Save an explicit argv list without a shell |
| `launch --job ID` | Reserve an attempt and start the detached wrapper |
| `status --job ID` | Read job state, matching receipt, and log location |
| `recover --job ID` | Reconcile saved receipts without launching work |
| `upgrade` | Add delivery tables without granting new authority |
| `configure-delivery --grant ID --socket PATH` | Record immutable continuation bounds and expected permissions |
| `arm --job ID --instructions TEXT` | Save one event and its substantive continuation |
| `run --job ID` | Launch the armed job and supervise delivery in one foreground call |
| `watch --job ID` | Supervise an existing job without relaunching it |
| `deliver --job ID` | Perform one bounded controller tick |
| `delivery-status --job ID` | Read the saved event without contacting Codex |
| `delivery-policy --grant ID` | Read continuation/check allowances and consumption |
| `reconcile-delivery --job ID` | Verify the acknowledged turn, or bounded discovery evidence, without sending a prompt |
| `revoke-delivery --grant ID` | Stop later reservations without killing work |
| `observe-progress --grant ID` | Copy existing worker events into private progress snapshots; never start a turn |
| `usage-status --grant ID` | Read fresh quota percentages and reset metadata from the configured server |

Without a project, `create-grant` requires `--workspace`. Defaults are one job,
60 seconds, and 1 MiB of command output. A project can set ceilings; explicit
options may lower them. Existing grants retain their original bounds.

`configure-delivery` defaults to one continuation and 120 checks. It expects
read-only, disabled tool network access, on-request approvals, and user review.
Use `--permissions-file PATH` for a different approved snapshot. It never changes
the worker's current settings. `run` and `watch` accept `--interval`, from 0.05 to
60 seconds, defaulting to two seconds. Checks and attempts persist across restarts.

`run` and `watch` accept `--progress` for automatic capture. Its independent
`--progress-duration` and `--progress-max-events` bounds default to 900 seconds
and 10,000 received events. `observe-progress` accepts `--duration`, `--max-events`,
and optionally `--job ID`; it can cover an engineering window before job launch.
Capture needs an existing delivery policy for the exact binding. It consumes no
job, continuation, or delivery-check allowance. See [progress capture](progress.md)
for paths, stopping behavior, and limits.

`configure-delivery --max-usage-resumes N` authorizes up to N usage recoveries,
defaulting to zero. Each also consumes a total continuation; both allowances must
remain available. Profile ceilings apply. Schema 3 is required for recovery;
`upgrade` preserves old counters and does not enable it. See [usage limits](usage-limits.md)
for reset waiting, interruption status, and recovery scope.

Exit code 0 means the command returned a record, not that a job or engineering task
succeeded. Read `observed_state` or delivery `state` in the JSON. Invalid arguments,
refused operations, and local errors return code 2. A blocked, failed, or uncertain
event always needs interpretation; it is not permission to retry or expand scope.

Full workflows: [local runner](job-runner.md), [project skill](../skills/bounded-workflow/references/usage.md),
and [completion delivery](completion-delivery.md).
