# Codex Scheduler

A small scheduler for bounded Codex engineering work. The goal is to let an
engineering agent start a job, yield while it runs, and continue when the result
is ready, without model calls spent checking progress.

The workflow separates the user conversation from a persistent engineering
worker. Ordinary code tracks jobs and delivers completion events to that worker.
The worker keeps engineering context and judgment; the scheduler handles waiting.

## Status

Early development. A [local job runner](docs/job-runner.md) now registers commands,
enforces runtime and log limits, and reconciles durable completion records. It runs
from the source checkout.
A [worker lifecycle experiment](docs/worker-lifecycle.md) tests completion delivery
to a dedicated worker thread. The first local trial passed: the worker yielded,
received one queued completion, retained context, and returned to idle.
[Bounded completion delivery](docs/completion-delivery.md) now connects the runner
to a dedicated worker with persistent allowances, revocation, and reconciliation.
The integrated live trial passed. It used one
job, one continuation, and two status checks, then stopped with the worker idle.

The [bounded-workflow skill](skills/bounded-workflow/SKILL.md) adds project setup
and handoff guidance. Its [usage guide](skills/bounded-workflow/references/usage.md)
explains how to use one runtime across projects with separate commands, bounds,
and local state. Profile inspection does not start work or authorize continuation.
Package installation and plugin distribution are still to come.

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

The local path is tested on macOS. Fewer manual polls is the intended benefit;
any reduction in model usage still needs to be measured.
