"""Codex Scheduler: record and run bounded local jobs."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .registry import Registry, SchedulerError
from .runner import launch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state/scheduler"))
    commands = parser.add_subparsers(dest="action", required=True)
    grant = commands.add_parser("create-grant", help="Record an explicit, bounded task authorization")
    grant.add_argument("--workspace", type=Path, required=True)
    grant.add_argument("--thread", required=True, help="Exact worker thread; never newest/last")
    grant.add_argument("--scope", required=True)
    grant.add_argument("--max-jobs", type=int, default=1)
    grant.add_argument("--timeout", type=float, default=60)
    grant.add_argument("--max-log-bytes", type=int, default=1048576)
    register = commands.add_parser("register", help="Save a job before launching it")
    register.add_argument("--grant", required=True)
    register.add_argument("command", nargs=argparse.REMAINDER)
    for name in ("launch", "status", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
    show_grant = commands.add_parser("grant-status")
    show_grant.add_argument("--grant", required=True)
    commands.add_parser("upgrade", help="Add delivery tables without changing existing allowances")
    configure = commands.add_parser("configure-delivery", help="Record a new, explicit continuation allowance")
    configure.add_argument("--grant", required=True)
    configure.add_argument("--socket", type=Path, required=True)
    configure.add_argument("--max-continuations", type=int, default=1)
    configure.add_argument("--max-checks", type=int, default=120)
    configure.add_argument("--permissions-file", type=Path)
    arm = commands.add_parser("arm", help="Save continuation instructions before yielding")
    arm.add_argument("--job", required=True)
    arm.add_argument("--instructions", required=True)
    for name in ("deliver", "reconcile-delivery", "delivery-status", "watch"):
        command = commands.add_parser(name)
        command.add_argument("--job", required=True)
        if name == "watch":
            command.add_argument("--interval", type=float, default=2)
    for name in ("revoke-delivery", "delivery-policy"):
        command = commands.add_parser(name)
        command.add_argument("--grant", required=True)
    args = parser.parse_args(argv)
    try:
        registry = Registry(args.state_dir, create=args.action in {"create-grant", "upgrade"})
        if args.action == "create-grant":
            result = registry.create_grant(
                workspace=args.workspace, worker_thread=args.thread, scope=args.scope,
                max_jobs=args.max_jobs, timeout_seconds=args.timeout, max_log_bytes=args.max_log_bytes,
            )
        elif args.action == "register":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
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
                    args.grant, socket_path=args.socket, max_continuations=args.max_continuations,
                    max_checks=args.max_checks, permissions=permissions,
                )
            elif args.action == "arm":
                result = store.arm(args.job, args.instructions)
            elif args.action == "revoke-delivery":
                result = store.revoke(args.grant)
            elif args.action == "delivery-policy":
                result = store.policy(args.grant)
            elif args.action == "delivery-status":
                result = store.event(args.job)
            else:
                dispatcher = Dispatcher(registry, CodexAdapter)
                result = (dispatcher.watch(args.job, interval=args.interval) if args.action == "watch"
                          else dispatcher.step(args.job, reconcile_only=args.action == "reconcile-delivery"))
        print(json.dumps(result, indent=2))
        return 0
    except (SchedulerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"Codex Scheduler: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
