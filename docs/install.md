# Install and try a local job

Install from the repository as shown in the [README](../README.md), or install a
wheel you built locally. There is no package-index publication step in this
project's release check. Keep the chosen environment active when changing projects.

The runtime requires Python 3.11 or newer and POSIX process/file-lock support.
The offline suite is tested on macOS with Python 3.11 and 3.14. Automatic delivery
also needs the `codex` extra, Codex CLI 0.164.0-compatible APIs, and an exclusively
owned worker on a local Unix socket. Installation does not provision that worker
or install a service.

## Offline example

In a disposable directory, export the bundled profile:

```sh
codex-scheduler export-profile scheduler.toml
codex-scheduler --project scheduler.toml project-info
```

No state is created by those reads. Add `.codex-scheduler/` to Git exclusions if
this directory is a repository. Create a one-job grant and register a harmless
command. `offline-demo` is deliberately fictional metadata for this example;
automatic delivery is not configured and no worker will be contacted.

```sh
codex-scheduler --project scheduler.toml create-grant \
  --thread offline-demo --scope "Print a small sum once"
```

Replace `GRANT_ID` with the returned identity:

```sh
codex-scheduler --project scheduler.toml register --grant GRANT_ID -- \
  python3 -c 'print(sum(range(10)))'
```

Replace `JOB_ID` with the returned job identity:

```sh
codex-scheduler --project scheduler.toml launch --job JOB_ID
```

The command returns while a detached wrapper runs the job. Inspect it later:

```sh
codex-scheduler --project scheduler.toml status --job JOB_ID
codex-scheduler --project scheduler.toml grant-status --grant GRANT_ID
```

Success requires a matching receipt. Status includes its exit code and a private
log path; the example prints `45`. The launch allowance remains consumed even
after failure or restart. Repeating `launch` for the same job is refused.

## Enable the skill

Export the complete skill folder to a supported discovery location:

```sh
codex-scheduler export-skill ~/.agents/skills/bounded-workflow
```

Use `$bounded-workflow` in Codex. Keep the runtime available in the selected Python
environment. The bundled helper works with an installed runtime; source checkout
selection remains available through `--runtime` or `CODEX_SCHEDULER_ROOT`.

Exporting refuses an existing folder. Review an older installation before
replacing it. No global installation or update happens automatically.

## Enable completion only for an approved task

Use the [skill guide](../skills/bounded-workflow/references/usage.md) to bind the
real dedicated worker and record finite allowances. Its thread ID, socket, and
permission snapshot must be actual local bindings, not the offline example's
metadata. Use `run` to launch the armed job and its waiter in one tool call.

For a configured task, stop future reservations with:

```sh
codex-scheduler --project scheduler.toml revoke-delivery --grant GRANT_ID
```

Revocation does not kill commands or interrupt already active turns. A reserved
attempt stays consumed. `recover` checks job receipts; `reconcile-delivery` checks
worker history without sending a new prompt. See the [delivery guide](completion-delivery.md)
for uncertain outcomes and approval handoffs.
