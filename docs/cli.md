# CLI reference

Use `codex-scheduler --help` or append `--help` to a subcommand for its flags.
`python -m codex_scheduler` is equivalent in the same environment.

Choose `--project PROFILE` or `--state-dir DIRECTORY` before the subcommand. They
are mutually exclusive. A profile supplies workspace paths and ceilings; without
one, records default to `state/scheduler/`. `--version` reports package metadata.

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
| `reconcile-delivery --job ID` | Inspect bounded worker history without sending a prompt |
| `revoke-delivery --grant ID` | Stop later reservations without killing work |

Without a project, `create-grant` requires `--workspace`. Defaults are one job,
60 seconds, and 1 MiB of command output. A project can set ceilings; explicit
options may lower them. Existing grants retain their original bounds.

`configure-delivery` defaults to one continuation and 120 checks. It expects
read-only, disabled tool network access, on-request approvals, and user review.
Use `--permissions-file PATH` for a different approved snapshot. It never changes
the worker's current settings. `run` and `watch` accept `--interval`, from 0.05 to
60 seconds, defaulting to two seconds. Checks and attempts persist across restarts.

Exit code 0 means the command returned a record, not that a job or engineering task
succeeded. Read `observed_state` or delivery `state` in the JSON. Invalid arguments,
refused operations, and local errors return code 2. A blocked, failed, or uncertain
event always needs interpretation; it is not permission to retry or expand scope.

Full workflows: [local runner](job-runner.md), [project skill](../skills/bounded-workflow/references/usage.md),
and [completion delivery](completion-delivery.md).
