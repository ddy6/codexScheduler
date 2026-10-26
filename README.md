# Codex Scheduler

A small scheduler for bounded Codex engineering work. The goal is to let an
engineering agent start a job, yield while it runs, and continue when the result
is ready, without model calls spent checking progress.

The proposed setup separates the user conversation from a persistent engineering
worker. Ordinary code tracks jobs and delivers completion events to that worker.
The worker keeps engineering context and judgment; the scheduler handles waiting.

## Status

Early development. A [local job runner](docs/job-runner.md) now registers commands,
enforces runtime and log limits, and reconciles durable completion records. It runs
from the source checkout; there is no installable scheduler or skill yet.
A [worker lifecycle experiment](docs/worker-lifecycle.md) tests completion delivery
to a dedicated worker thread. The first local trial passed: the worker yielded,
received one queued completion, retained context, and returned to idle.
The runner and worker experiment are separate for now; automatic delivery is the
next slice.

## Design

- A Python package launches jobs and stores durable status and completion records.
- A Codex skill explains delegation, registration, handoffs, and stopping rules.
- Project configuration supplies workspace details and operating limits.
- Plugin packaging can follow once the local workflow is proven.

Automatic continuation stays within an approved task and a finite allowance.
Restarts must preserve consumed allowance. Ambiguous delivery, new permissions,
or a decision outside the task stops for human review. The initial scope is local
engineering verification, with one engineering writer per workspace.

## Development

Use synthetic jobs and disposable workspaces for integration checks. Changes to
job state, delivery, or recovery need focused tests, including interrupted and
duplicate delivery cases. Broader checks belong at meaningful checkpoints.

Keep private prompts, session identifiers, credentials, machine paths, and runtime
records out of source and examples. Use relative paths or clearly fictional
values. Public behavior and setup documentation belong in this README or `docs/`;
local development plans and agent guidance are ignored by Git.

Compatibility and installation instructions will be documented after the first
working prototype. Fewer manual polls is the intended benefit; any reduction in
model usage still needs to be measured.
