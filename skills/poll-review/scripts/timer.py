#!/usr/bin/env python3
"""Local deadline evidence and guarded review reads; never contacts an agent."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


MAX_SECONDS = 7 * 24 * 60 * 60
MAX_RECORD_BYTES = 16384
MAX_REVIEW_BYTES = 32768
BINDING_FIELDS = ("task", "generation", "cycle", "root", "helper", "job_kind", "job_handle")


def bounded_read(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds its byte limit")
    return data


def boot_identity():
    """Fail closed when this host cannot identify the current boot."""
    system = platform.system()
    if system == "Linux":
        value = bounded_read("/proc/sys/kernel/random/boot_id", 128).decode().strip()
    elif system == "Darwin":
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, check=True, timeout=2,
        )
        value = result.stdout.strip()
    else:
        raise ValueError("timer supports macOS and Linux; use host timing on other systems")
    if not value or len(value) > 256:
        raise ValueError("boot identity unavailable; reconcile the timer")
    return system + ":" + value


def clock_sample():
    boot = boot_identity()
    mono = time.monotonic_ns()
    return {"boot": boot, "mono_ns": mono, "wall_ns": time.time_ns()}


def finite_number(value, low, high):
    return (type(value) in (int, float) and math.isfinite(value)
            and low <= value <= high)


def validate(record):
    if not isinstance(record, dict) or record.get("version") != 1:
        raise ValueError("unsupported timer record")
    binding = record.get("binding")
    if not isinstance(binding, dict) or set(binding) != set(BINDING_FIELDS):
        raise ValueError("timer needs an exact handoff binding")
    for value in binding.values():
        if (not isinstance(value, str) or not value.strip()
                or len(value.encode("utf-8")) > 256
                or any(ord(char) < 32 for char in value)):
            raise ValueError("binding fields must be short, nonempty single-line strings")
    if not finite_number(record.get("seconds"), 0.001, MAX_SECONDS):
        raise ValueError("seconds must be finite, from 0.001 through 604800")
    if not finite_number(record.get("margin_seconds"), 0, 60):
        raise ValueError("review margin must be finite, from 0 through 60 seconds")
    clock = record.get("clock")
    if (not isinstance(clock, dict) or not isinstance(clock.get("boot"), str)
            or not clock["boot"] or len(clock["boot"]) > 512
            or any(type(clock.get(key)) is not int or clock[key] < 0
                   for key in ("mono_ns", "wall_ns"))):
        raise ValueError("invalid clock anchor")
    if (type(record.get("deadline_ns")) is not int
            or record["deadline_ns"] != clock["mono_ns"] + int(record["seconds"] * 1e9)):
        raise ValueError("invalid timer deadline")
    return record


def write_new(path, value):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def start(directory, binding, seconds, margin=10):
    sample = clock_sample()
    # Validate all inputs before reserving an identity on disk.
    record = validate({
        "version": 1, "binding": binding, "seconds": seconds,
        "margin_seconds": margin, "clock": sample,
        "deadline_ns": sample["mono_ns"] + int(seconds * 1e9)
        if finite_number(seconds, 0.001, MAX_SECONDS) else None,
    })
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_new(directory / "timer.json", record)
    return record


def load(directory):
    return validate(json.loads(bounded_read(Path(directory) / "timer.json", MAX_RECORD_BYTES)))


def status(directory):
    directory = Path(directory)
    record = load(directory)
    result = {"binding": record["binding"], "review_allowed": False,
              "wait_claimed": (directory / "wait.claim").exists()}
    if (directory / "cancelled").exists():
        return {**result, "state": "cancelled", "due": False}
    sample = clock_sample()
    anchor = record["clock"]
    elapsed = sample["mono_ns"] - anchor["mono_ns"]
    wall_elapsed = sample["wall_ns"] - anchor["wall_ns"]
    if (sample["boot"] != anchor["boot"] or elapsed < 0
            or abs(wall_elapsed - elapsed) > 2_000_000_000):
        raise ValueError("clock continuity lost; reconcile without restarting the timer")
    remaining = (record["deadline_ns"] - sample["mono_ns"]) / 1e9
    due = remaining <= 0
    return {**result, "state": "due" if due else "waiting", "due": due,
            "remaining_seconds": max(0, remaining),
            "late_seconds": max(0, -remaining),
            "review_allowed": remaining > record["margin_seconds"]}


def cancel(directory):
    directory = Path(directory)
    record = load(directory)
    try:
        write_new(directory / "cancelled", {"binding": record["binding"]})
    except FileExistsError:
        pass
    return {"state": "cancelled", "due": False, "binding": record["binding"]}


def wait(directory, chunk_seconds=60):
    """One waiter per record, including after a crash. A claim is not liveness."""
    if not finite_number(chunk_seconds, 0.001, 60):
        raise ValueError("sleep chunks must be finite, from 0.001 through 60 seconds")
    directory = Path(directory)
    record = load(directory)
    status(directory)  # Refuse invalid clocks before consuming the wait attempt.
    write_new(directory / "wait.claim", {"binding": record["binding"]})
    try:
        while True:
            result = status(directory)
            if result["state"] != "waiting":
                break
            time.sleep(min(chunk_seconds, result["remaining_seconds"]))
    except KeyboardInterrupt:
        result = cancel(directory)
    # An incomplete write or missing outcome requires inspection, never another wait.
    write_new(directory / "outcome.json", result)
    return result


def read_review(directory, snapshot, sha256):
    before = status(directory)
    if not before["review_allowed"]:
        return {"review_allowed": False, "timer": before}
    if (len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256)):
        raise ValueError("provide the engineer's SHA-256 for the frozen snapshot")
    raw = bounded_read(snapshot, MAX_REVIEW_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("snapshot hash mismatch; request a current frozen batch")
    source = raw.decode("utf-8")
    after = status(directory)
    if not after["review_allowed"]:
        return {"review_allowed": False, "timer": after}
    return {"review_allowed": True, "timer": after, "sha256": sha256,
            "snapshot": str(snapshot), "source": source}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("start", help="Anchor a deadline in a new private directory")
    create.add_argument("--record", type=Path, required=True)
    create.add_argument("--seconds", type=float, required=True)
    create.add_argument("--margin", type=float, default=10)
    for field in BINDING_FIELDS:
        create.add_argument("--" + field.replace("_", "-"), required=True)
    for name in ("status", "wait", "cancel", "read-review"):
        command = commands.add_parser(name)
        command.add_argument("--record", type=Path, required=True)
        if name == "wait":
            command.add_argument("--chunk-seconds", type=float, default=60)
        if name == "read-review":
            command.add_argument("--snapshot", type=Path, required=True)
            command.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            result = start(args.record, {key: getattr(args, key) for key in BINDING_FIELDS},
                           args.seconds, args.margin)
        elif args.command == "wait":
            result = wait(args.record, args.chunk_seconds)
        elif args.command == "read-review":
            result = read_review(args.record, args.snapshot, args.sha256)
        else:
            result = {"status": status, "cancel": cancel}[args.command](args.record)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "action": "reconcile; do not retry effects"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
