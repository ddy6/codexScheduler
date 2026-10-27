"""Codex Scheduler: record and run bounded local jobs."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import sys

from .registry import Registry, SchedulerError
from .runner import launch
from .project import DEFAULT_LIMITS, Project
from .assets import export_profile, export_skill, package_version
from .transport import receive_limit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="codex-scheduler " + package_version())
    parser.add_argument("--max-receive-bytes", type=int, help="Finite Codex receive limit; override transport only, without changing task allowance")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--state-dir", type=Path)
    location.add_argument("--project", type=Path, help="Explicit TOML profile; paths resolve from the profile")
    commands = parser.add_subparsers(dest="action", required=True)
    grant = commands.add_parser("create-grant", help="Record an explicit, bounded task authorization")
    grant.add_argument("--workspace", type=Path, help="Required without a project profile")
    grant.add_argument("--thread", required=True, help="Exact worker thread; never newest/last")
    grant.add_argument("--scope", required=True)
    grant.add_argument("--max-jobs", type=int)
    grant.add_argument("--timeout", type=float)
    grant.add_argument("--max-log-bytes", type=int)
    register = commands.add_parser("register", help="Save a job before launching it")
    register.add_argument("--grant", required=True)
    register.add_argument("--command", help="Named command from the project profile")
    register.add_argument("argv", nargs=argparse.REMAINDER)
    for name in ("launch", "status", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
    show_grant = commands.add_parser("grant-status")
    show_grant.add_argument("--grant", required=True)
    commands.add_parser("upgrade", help="Add delivery tables without changing existing allowances")
    configure = commands.add_parser("configure-delivery", help="Record a new, explicit continuation allowance")
    configure.add_argument("--grant", required=True)
    configure.add_argument("--socket", type=Path, required=True)
    configure.add_argument("--max-continuations", type=int)
    configure.add_argument("--max-checks", type=int)
    configure.add_argument("--max-usage-resumes", type=int, help="Explicit recovery allowance within the total continuation budget; default 0")
    configure.add_argument("--permissions-file", type=Path)
    arm = commands.add_parser("arm", help="Save continuation instructions before yielding")
    arm.add_argument("--job", required=True)
    arm.add_argument("--instructions", required=True)
    for name in ("deliver", "reconcile-delivery", "delivery-status", "watch", "run"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        if name in {"watch", "run"}:
            command.add_argument("--interval", type=float, default=2)
            command.add_argument("--progress", action="store_true", help="Capture existing worker events without model calls")
            command.add_argument("--progress-duration", type=float, default=900)
            command.add_argument("--progress-max-events", type=int, default=10000)
    observe = commands.add_parser("observe-progress", help="Capture an exact worker's existing events; never start a turn")
    observe.add_argument("--grant", required=True)
    observe.add_argument("--job", help="Optional exact job to include in the snapshot")
    observe.add_argument("--duration", type=float, default=900)
    observe.add_argument("--max-events", type=int, default=10000)
    for name in ("revoke-delivery", "delivery-policy", "usage-status"):
        command = commands.add_parser(name)
        command.add_argument("--grant", required=True)
    commands.add_parser("project-info", help="Inspect a profile without creating state or starting work")
    for name in ("export-skill", "export-profile"):
        command = commands.add_parser(name, help="Copy a bundled setup asset; refuse an existing destination")
        command.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action in {"export-skill", "export-profile"}:
            result = export_skill(args.destination) if args.action == "export-skill" else export_profile(args.destination)
            print(json.dumps(result, indent=2))
            return 0
        project = Project.load(args.project) if args.project else None
        if args.max_receive_bytes is not None:
            receive_limit(args.max_receive_bytes)
            if project:
                project.limit("max_receive_bytes", args.max_receive_bytes)
        if args.action == "project-info":
            if project is None:
                raise SchedulerError("project-info requires --project")
            print(json.dumps(project.info(), indent=2))
            return 0
        state_dir = project.state_dir if project else args.state_dir or Path("state/scheduler")
        # Resolve and validate project bounds before initializing any registry.
        def bound(name, requested):
            return project.limit(name, requested) if project else DEFAULT_LIMITS[name] if requested is None else requested

        if args.action == "create-grant":
            if project and args.workspace and args.workspace.resolve() != project.workspace:
                raise SchedulerError("Explicit workspace disagrees with the project profile")
            workspace = project.workspace if project else args.workspace
            if workspace is None:
                raise SchedulerError("create-grant requires --workspace or --project")
            max_jobs = bound("max_jobs", args.max_jobs)
            timeout = bound("timeout_seconds", args.timeout)
            log_bytes = bound("max_log_bytes", args.max_log_bytes)
        elif args.action == "configure-delivery":
            continuations = bound("max_continuations", args.max_continuations)
            checks = bound("max_checks", args.max_checks)
            usage_resumes = bound("max_usage_resumes", args.max_usage_resumes)
            receive_bytes = bound("max_receive_bytes", args.max_receive_bytes)
        registry = Registry(state_dir, create=args.action in {"create-grant", "upgrade"})
        if project and hasattr(args, "grant"):
            project.check_grant(registry, args.grant)
        if project and hasattr(args, "job"):
            project.check_job(registry, args.job)
        if args.action == "create-grant":
            result = registry.create_grant(
                workspace=workspace, worker_thread=args.thread, scope=args.scope,
                max_jobs=max_jobs, timeout_seconds=timeout, max_log_bytes=log_bytes,
            )
        elif args.action == "register":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            if args.command:
                if project is None or args.command not in project.commands or command:
                    raise SchedulerError("Select one known project command or supply an explicit argv list")
                command = project.commands[args.command]
            result = registry.register(args.grant, command)
        elif args.action == "launch":
            result = launch(registry, args.job)
        elif args.action == "status":
            result = registry.inspect(args.job)
        elif args.action == "recover":
            result = registry.recover(args.job)
        elif args.action == "grant-status":
            result = registry.grant(args.grant)
        elif args.action == "upgrade":
            result = {"schema_version": registry.version}
        else:
            from .codex import CodexAdapter
            from .delivery import DeliveryStore, Dispatcher

            def adapter_factory(spec):
                if args.max_receive_bytes is not None:
                    spec = {**spec, "max_receive_bytes": args.max_receive_bytes}
                return CodexAdapter(spec)

            store = DeliveryStore(registry)
            if args.action == "configure-delivery":
                permissions = None
                if args.permissions_file:
                    with args.permissions_file.open("rb") as stream:
                        data = stream.read(16385)
                    if len(data) > 16384:
                        raise SchedulerError("Permissions snapshot exceeds 16 KiB")
                    permissions = json.loads(data)
                result = store.configure(
                    args.grant, socket_path=args.socket, max_continuations=continuations,
                    max_checks=checks, permissions=permissions, max_usage_resumes=usage_resumes,
                    max_receive_bytes=receive_bytes,
                )
            elif args.action == "arm":
                result = store.arm(args.job, args.instructions)
            elif args.action == "revoke-delivery":
                result = store.revoke(args.grant)
            elif args.action == "delivery-policy":
                result = store.policy(args.grant)
            elif args.action == "delivery-status":
                result = store.event(args.job)
            elif args.action == "usage-status":
                from .usage import read_quota

                policy = store.policy(args.grant)
                with adapter_factory(policy["spec"]) as adapter:
                    result = {"grant_id": args.grant, "quota": read_quota(adapter)}
            elif args.action == "observe-progress":
                from .progress import ProgressObserver

                observer = ProgressObserver(registry, args.grant, job_id=args.job, duration=args.duration,
                                            max_events=args.max_events, adapter_factory=adapter_factory)
                result = {**observer.run(), **observer.locations()}
            else:
                dispatcher = Dispatcher(registry, adapter_factory)
                if args.action == "run":
                    if not 0.05 <= args.interval <= 60:
                        raise SchedulerError("Polling interval must be between 0.05 and 60 seconds")
                    CodexAdapter.require_available()
                    event = store.event(args.job)
                    policy = store.policy(event["grant_id"])
                    if (event["state"] != "pending" or event["attempts"] or policy["revoked_at"]
                            or policy["consumed_continuations"] >= policy["spec"]["max_continuations"]
                            or policy["consumed_checks"] >= policy["spec"]["max_checks"]):
                        raise SchedulerError("run requires an armed, unused continuation allowance")
                observer = None
                if args.action in {"run", "watch"} and args.progress:
                    from .progress import ProgressObserver

                    event = store.event(args.job)
                    observer = ProgressObserver(registry, event["grant_id"], job_id=args.job,
                                                duration=args.progress_duration, max_events=args.progress_max_events,
                                                adapter_factory=adapter_factory)
                with observer.background() if observer else nullcontext():
                    if args.action == "run":
                        launch(registry, args.job)
                    result = (dispatcher.watch(args.job, interval=args.interval) if args.action in {"watch", "run"}
                              else dispatcher.step(args.job, reconcile_only=args.action == "reconcile-delivery"))
                if observer:
                    result["progress"] = {**observer.locations(), "observer_state": observer.record["observer_state"]}
        print(json.dumps(result, indent=2))
        return 0
    except (SchedulerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"Codex Scheduler: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
