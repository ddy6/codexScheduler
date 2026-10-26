"""Reach a trusted Codex Scheduler checkout without copying it into a project."""

import argparse
import os
from pathlib import Path
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=os.environ.get("CODEX_SCHEDULER_ROOT"),
                        help="Trusted runtime checkout; defaults to CODEX_SCHEDULER_ROOT")
    try:
        split = argv.index("--")
    except ValueError:
        if argv in (["--help"], ["-h"]):
            parser.print_help()
            return 0
        parser.error("Separate launcher options and runtime arguments with --")
    args = parser.parse_args(argv[:split])
    if args.runtime is None:
        parser.error("Supply --runtime or CODEX_SCHEDULER_ROOT")
    root = Path(args.runtime).resolve()
    if not (root / "codex_scheduler" / "__main__.py").is_file():
        parser.error("Runtime checkout must contain codex_scheduler/__main__.py")
    sys.path.insert(0, str(root))
    from codex_scheduler.__main__ import main as scheduler_main

    return scheduler_main(argv[split + 1:])


if __name__ == "__main__":
    raise SystemExit(main())
