# Codex Scheduler

A small toolkit for engineering agents that need to yield during long jobs.
Start with a focused reviewer/poller skill and a standalone timer. Use the durable
Codex scheduler when you need job receipts, persisted budgets and completion delivery.

| Workflow | What it provides | Requirements |
| --- | --- | --- |
| [poll-review](skills/poll-review/SKILL.md) | One small-context helper reviews frozen batches and owns poll deadlines | Authorized team; host-specific exact-root return route for automatic wakes |
| [Standalone timer](skills/poll-review/references/timer.md) | Monotonic deadline, cancellable wait and guarded snapshot reads | Python 3.11+, macOS/Linux boot-identity access; no package dependencies |
| [bounded-workflow](skills/bounded-workflow/SKILL.md) | Durable jobs, grants, receipts and bounded Codex delivery | Installed runtime; dedicated worker for automatic continuation |

The lightweight role instructions are model-agnostic and can be installed as a
skill or referenced from local guidance. Small context is an input and lifecycle
discipline, not a model choice. Automatic root wakes depend on the host's available
tools and permissions; the timer produces evidence, not messages or job results.
Only one helper or dispatcher may own a job's continuation.

## Install the lightweight skill

From a trusted checkout, copy the complete standalone skill to Codex's user skill
directory (the copy refuses an existing destination):

```sh
git clone https://github.com/ddy6/codexScheduler.git
cd codexScheduler
python3 -c 'from pathlib import Path; import shutil; shutil.copytree("skills/poll-review", Path.home() / ".agents/skills/poll-review")'
```

Use `$poll-review` for an authorized team. For other hosts, copy the same folder
to their skill location or reference its instructions from local project guidance.
See [host setup and capability checks](skills/poll-review/references/hosts.md).
No scheduler package or Codex server is required for the skill and timer.

For long waits, the helper anchors a deadline, completes any assigned review outside
the safety margin, then lets ordinary code sleep until the original deadline.
See the [timer commands and recovery limits](skills/poll-review/references/timer.md).
Installing these files does not start agents, services, jobs or automatic continuation.

## Install the durable scheduler

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

The [quick start](docs/install.md) runs an offline example. To make the scheduler skill
available across local Codex projects:

```sh
codex-scheduler export-skill ~/.agents/skills/bounded-workflow
```

The installed package also exports the standalone skill:

```sh
codex-scheduler export-skill ~/.agents/skills/poll-review --name poll-review
```

Exports refuse existing destinations. Reading a profile or discovering the skill
does not create a grant or activate continuation.

## Status

This is a local alpha. The lightweight skill and timer have offline validation;
live helper activation, automatic root delivery and token savings are not established
by those checks. The timer supports macOS and Linux; its clock-binding read can be
blocked by a host sandbox. Other-agent integrations and Windows timing are unverified.

The durable runner has grants, runtime/log limits, receipt
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
- A portable skill explains small-context review, deadline priority and exact-target handoffs.
- A standalone timer supplies due evidence without an agent transport.
- A separate Codex skill explains durable registration, delivery and recovery.
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
network access; `--wheelhouse PATH` uses cached wheels for an offline check.
It does not start a Codex server or invoke a model.

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
