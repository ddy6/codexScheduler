# Codex Scheduler

A small scheduler for bounded Codex engineering work. The goal is to let an
engineering agent start a job, yield while it runs, and continue when the result
is ready, without model calls spent checking progress.

The workflow separates the user conversation from a persistent engineering
worker. Ordinary code tracks jobs and delivers completion events to that worker.
The worker keeps engineering context and judgment; the scheduler handles waiting.

## Install

Python 3.11 or newer and a POSIX host are required. The offline suite passes on
macOS with Python 3.11 and 3.14. Windows is unsupported. Install from a trusted checkout:

```sh
git clone https://github.com/ddy6/codexScheduler.git
cd codexScheduler
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ".[codex]"
codex-scheduler --version
```

Use `pip install .` if you only need the local runner. The `codex` extra adds the
WebSocket client for completion delivery; a signed-in local Codex CLI and a
dedicated worker are separate requirements.

In another project, export a profile and review its command and bounds:

```sh
codex-scheduler export-profile scheduler.toml
codex-scheduler --project scheduler.toml project-info
```

The [quick start](docs/install.md) runs an offline example. To make the skill
available across local Codex projects:

```sh
codex-scheduler export-skill ~/.agents/skills/bounded-workflow
```

Exports refuse existing destinations. Reading a profile or discovering the skill
does not create a grant or activate continuation.

## Status

This is a local alpha. The runner has durable grants, runtime/log limits, receipt
recovery, and bounded completion delivery. One integrated live trial completed a
job and resumed the same dedicated worker once, retaining context and stopping
at idle. Worker provisioning is still separate; native child agents, machine
reboot, long unattended runs, and competing interactive writers are unverified.

- [Skill and project setup](skills/bounded-workflow/references/usage.md)
- [CLI reference](docs/cli.md)
- [Completion, stopping, and recovery](docs/completion-delivery.md)
- [Architecture](docs/architecture.md)
- [Automatic progress capture](docs/progress.md)
- [Usage limits and recovery](docs/usage-limits.md)
- [Response bounds and confirmation](docs/response-limits.md)
- [Release checks and evidence](docs/release.md)

## Design

- A Python package launches jobs and stores durable status and completion records.
- A Codex skill explains delegation, registration, handoffs, and stopping rules.
- Project configuration supplies workspace details and operating limits.
- Plugin packaging can follow once the local workflow is proven.

Automatic continuation stays within an approved task and a finite allowance.
Restarts must preserve consumed allowance. Ambiguous delivery, new permissions,
or a decision outside the task stops for human review. The initial scope is local
engineering verification, with one engineering writer per workspace.

For a rolling local progress document, add `--progress` to `run` or `watch`.
Ordinary code copies existing worker events and job evidence without another
model call. The supervisor reads the snapshot when needed; reading or summarizing
it in a model turn still uses tokens.

Usage interruptions remain visible as unfinished work when a worker goes idle.
`usage-status` reads quota percentages and reset metadata. Automatic recovery of
scheduler-owned turns is opt-in and consumes the existing continuation budget.

## Development

```sh
python -m pip install -r requirements-dev.txt -r requirements-prototype.txt
python -m unittest discover -s tests -v
python -m tools.verify_release --work-dir state/release-check-001
```

The release check builds and audits a wheel and source archive, then exercises
fresh base and Codex-extra installations. Its optional dependency download needs
network access. It does not start a Codex server or invoke a model.

Use synthetic jobs and disposable workspaces for integration checks. Changes to
job state, delivery, or recovery need focused tests, including interrupted and
duplicate delivery cases. Broader checks belong at meaningful checkpoints.

Keep private prompts, session identifiers, credentials, machine paths, and runtime
records out of source and examples. Use relative paths or clearly fictional
values. Public behavior and setup documentation belong in this README or `docs/`;
local development plans and agent guidance are ignored by Git.

The local path is tested on macOS. Fewer manual polls is the intended benefit;
any reduction in model usage still needs to be measured.

## License

MIT. Copyright 2026 Looptimum (ddy6). See [LICENSE](LICENSE).
