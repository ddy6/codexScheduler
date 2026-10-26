"""A harmless job whose completion is released after its launching turn ends."""

import argparse
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    while not args.release.exists():
        if time.monotonic() >= deadline:
            raise SystemExit("The launching turn did not release this job in time.")
        time.sleep(0.05)
    print(json.dumps({"result": sum(range(10)), "status": "completed"}))


if __name__ == "__main__":
    main()
