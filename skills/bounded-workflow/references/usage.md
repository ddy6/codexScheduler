# Use the skill in another project

The skill uses an installed Codex Scheduler runtime or one explicitly selected
trusted source checkout. Its launcher never changes directories or copies runtime
code into a project. Python 3.11 or newer and local POSIX process access are
required. macOS is tested; Windows and hosted sessions without access to the local
runtime are unsupported.

## Make the skill discoverable

Keep the complete `bounded-workflow` folder, including `scripts/` and
`references/`. The installed runtime can export it for local discovery:

```sh
codex-scheduler export-skill ~/.agents/skills/bounded-workflow
```

The export refuses an existing destination and starts no work. A source-checkout
user can instead copy or symlink the complete folder into
`~/.agents/skills/bounded-workflow`. A project can instead use
`.agents/skills/bounded-workflow`. Do not overwrite an existing skill installation.
These steps make instructions available; they do not activate scheduling.

For example, from a source checkout:

```sh
mkdir -p ~/.agents/skills
ln -s /path/to/codex-scheduler/skills/bounded-workflow ~/.agents/skills/bounded-workflow
```

Invoke `$bounded-workflow` in Codex. Normal implicit discovery remains enabled.
User-scope discovery applies across repositories, while project-specific commands
and bounds stay in the project's profile. Plugin packaging comes later.

Discovery reference: [official skill documentation](https://learn.chatgpt.com/docs/build-skills).

## Prepare a project profile

Run `codex-scheduler export-profile scheduler.toml` in the target project, copy
`examples/project.toml` from a checkout, or write a profile for its verification
command. Exports refuse an existing file:

```toml
version = 1
workspace = "."
state_dir = ".codex-scheduler"

[commands]
verify = ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]

[limits]
max_jobs = 1
timeout_seconds = 60
max_log_bytes = 1048576
max_continuations = 1
max_checks = 120
```

The workspace is relative to the profile file. The state directory is relative to
the workspace and must stay inside it. Add `.codex-scheduler/` to the project's
Git exclusions before creating local records. Profiles can be shared if their
commands and paths are public; worker IDs, sockets, permissions snapshots, and
receipts belong in private state.

Command values are argv lists. The runtime does not expand environment variables
or interpret a command string as a shell script. Use the project's own executable
or environment when needed. Named commands are conveniences, not a security
allowlist. Review the actual command against the approved task.

Bounds are ceilings for newly created grants and delivery policies. CLI options
can lower them. Changing a profile never amends an existing grant or restores
consumed capacity. Reading a profile creates no state and grants no authority.

## Reach the shared runtime

With the installed environment active, use the CLI directly from any project:

```sh
codex-scheduler --project /path/to/project/scheduler.toml project-info
```

The helper in the loaded skill also works with the installed package:

```sh
python3 /path/to/bounded-workflow/scripts/scheduler.py -- \
  --project /path/to/project/scheduler.toml project-info
```

The `--` separates launcher options from runtime arguments, which are passed
unchanged. For an uninstalled source checkout, add `--runtime PATH` before that
separator, or set `CODEX_SCHEDULER_ROOT`. An explicit checkout takes precedence;
an invalid one is refused instead of silently selecting a different runtime.
Use an interpreter with the `codex` extra for automatic delivery. It is unnecessary
for profile inspection and ordinary runner commands.

For repeated commands from a project's root, this optional shell function keeps
examples short:

```sh
scheduler() {
  codex-scheduler --project ./scheduler.toml "$@"
}
scheduler project-info
```

The following commands use that optional function, not an installed executable
named `scheduler`. A Codex session can use the full CLI or helper prefix instead.

## Bind the worker and approved task

Automatic delivery needs an existing dedicated persistent worker, its exact thread
ID, a local app-server Unix socket, and its approved sandbox/approval snapshot.
Use the client's supported thread APIs or tooling to obtain these bindings.
Worker provisioning is separate from this skill. Do not guess the bindings,
select the newest session, or repurpose the live user conversation.

The controller requires exclusive access to its worker. The user-facing agent can
delegate engineering when authorized and return for decisions and final results.
Model choices are session or user choices; the runtime does not route models.
Native child-agent lifecycle behavior is unverified, so prefer a controller-owned
persistent thread. Without the required local binding, prepare the profile and
report what is missing instead of claiming that continuation is active.

With the task and continuation bounds already authorized, record a grant and
register the named command. Replace each ID with the exact returned value:

```sh
scheduler create-grant --thread WORKER_THREAD_ID --scope "Run one approved verification"
scheduler register --grant GRANT_ID --command verify
scheduler configure-delivery --grant GRANT_ID --socket /path/to/worker.sock
scheduler arm --job JOB_ID --instructions "Read the receipt, report the result, and stop."
```

The default permission snapshot expects read-only, disabled tool network access,
on-request approvals, and user review. For other approved settings, supply the
actual snapshot with `configure-delivery --permissions-file PATH`. Delivery
compares it to the current worker settings and never overrides them.

Registration and arming do not run the command. Start the job and its bounded
waiter together in one execution tool call:

```sh
scheduler run --job JOB_ID
```

`run` stays in the foreground while ordinary code supervises the job and delivery.
Use a tool that preserves a returned running process across turns. When it yields,
the agent ends its turn immediately. The waiter can later start only the bounded
continuation in the dedicated worker. It does not wake an agent merely to check a
running command. No service or recurring task is installed.

If `run` fails after launching, do not run it again to retry the job. Inspect the
saved state. `watch` can resume supervision of an existing job without relaunching
it; a reserved or uncertain delivery is reconciled without another send.

Without automatic-continuation authority, omit delivery configuration and arming.
Use `launch`, then inspect status only on the user's next poll. Do not start a
waiter or manufacture a worker identity to fill a missing binding.

## Stop and recover

```sh
scheduler status --job JOB_ID
scheduler delivery-status --job JOB_ID
scheduler grant-status --grant GRANT_ID
scheduler delivery-policy --grant GRANT_ID
scheduler recover --job JOB_ID
scheduler reconcile-delivery --job JOB_ID
scheduler revoke-delivery --grant GRANT_ID
```

Status reads do not start work. `recover` reconciles command receipts;
`reconcile-delivery` checks worker history without sending a prompt. Unknown or
stale evidence is not success. Revocation stops future reservations without
killing commands or interrupting already active turns. Exhausted checks,
unavailable approval, changed permissions, and uncertain sends stop for operator
review; they never refill budgets or trigger automatic retries.

One armed or unresolved event owns its worker and workspace. Finish with a handoff
before starting a successor. A completed delivery means the turn ended, not that
the engineer's substantive task is done. Read its result and remaining scope.

## What has been checked

The same helper and runtime executed different verification commands in two
disposable projects, from an unrelated working directory with spaces in its path.
Each project kept separate state and rejected the other project's job identity.
Profile inspection created no registry and neither profile activated delivery.
At the skill checkpoint, all 93 offline tests and the skill-creator validator
passed. The one-call
handoff ran a real local job with a simulated worker. The runner/dispatcher's live
normal path was verified separately in the bounded worker trial. The skill's model
selection and implicit activation have not been tested in a live Codex
conversation; skill validation is structural.

The repository's release check additionally tests wheel-installed asset exports
and this helper from an unrelated project with no checkout path. The installed
runtime does not provision a worker or activate scheduling by itself.
