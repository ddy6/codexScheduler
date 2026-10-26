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
    args = parser.parse_args(argv)
    try:
        registry = Registry(args.state_dir, create=args.action == "create-grant")
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
        else:
            result = registry.grant(args.grant)
        print(json.dumps(result, indent=2))
        return 0
    except (SchedulerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"Codex Scheduler: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
