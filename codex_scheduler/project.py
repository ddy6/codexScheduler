"""Explicit project profiles: paths, named commands, and bounds, never grants."""

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tomllib

from .registry import SchedulerError


DEFAULT_LIMITS = {
    "max_jobs": 1, "timeout_seconds": 60, "max_log_bytes": 1048576,
    "max_continuations": 1, "max_checks": 120,
}
MAX_LIMITS = {
    "max_jobs": 1000, "timeout_seconds": 86400, "max_log_bytes": 104857600,
    "max_continuations": 1000, "max_checks": 10000,
}


def valid_limit(name, value):
    if name == "timeout_seconds":
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAX_LIMITS[name]
    else:
        valid = type(value) is int and 1 <= value <= MAX_LIMITS[name]
    if not valid:
        raise SchedulerError(f"Invalid project limit: {name}")
    return value


@dataclass(frozen=True)
class Project:
    path: Path
    workspace: Path
    state_dir: Path
    commands: dict
    limits: dict

    @classmethod
    def load(cls, filename):
        path = Path(filename).resolve(strict=True)
        with path.open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise SchedulerError("Project profile exceeds 64 KiB")
        try:
            config = tomllib.loads(data.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise SchedulerError("Invalid project TOML") from error
        allowed = {"version", "workspace", "state_dir", "commands", "limits"}
        if set(config) - allowed or type(config.get("version")) is not int or config["version"] != 1:
            raise SchedulerError("Expected project profile version 1 with known fields")
        workspace_text = config.get("workspace", ".")
        state_text = config.get("state_dir", ".codex-scheduler")
        if any(not isinstance(value, str) or not value or "\0" in value
               for value in (workspace_text, state_text)):
            raise SchedulerError("Project paths must be nonempty strings")
        workspace = (path.parent / workspace_text).resolve(strict=True)
        if not workspace.is_dir():
            raise SchedulerError("Project workspace must be a directory")
        if Path(state_text).is_absolute():
            raise SchedulerError("Project state_dir must be relative to its workspace")
        state_dir = (workspace / state_text).resolve()
        if state_dir == workspace or not state_dir.is_relative_to(workspace):
            raise SchedulerError("Project state_dir must remain inside its workspace")
        commands = config.get("commands", {})
        limits = config.get("limits", {})
        if not isinstance(commands, dict) or len(commands) > 32:
            raise SchedulerError("Expected at most 32 named project commands")
        for name, argv in commands.items():
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
                    or not isinstance(argv, list) or not argv or len(argv) > 256
                    or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                    or not argv[0] or sum(map(len, argv)) > 65536):
                raise SchedulerError(f"Invalid argv list for project command: {name}")
        if not isinstance(limits, dict) or set(limits) - DEFAULT_LIMITS.keys():
            raise SchedulerError("Unknown project limits")
        resolved = {**DEFAULT_LIMITS, **limits}
        for name, value in resolved.items():
            valid_limit(name, value)
        return cls(path, workspace, state_dir, commands, resolved)

    def limit(self, name, requested=None):
        value = self.limits[name] if requested is None else valid_limit(name, requested)
        if value > self.limits[name]:
            raise SchedulerError(f"Requested {name} exceeds the project bound")
        return value

    def check_grant(self, registry, grant_id):
        if Path(registry.grant(grant_id)["workspace"]).resolve() != self.workspace:
            raise SchedulerError("Grant belongs to a different project workspace")

    def check_job(self, registry, job_id):
        if Path(registry.job(job_id)["registration"]["workspace"]).resolve() != self.workspace:
            raise SchedulerError("Job belongs to a different project workspace")

    def info(self):
        return {"profile": str(self.path), "workspace": str(self.workspace),
                "state_dir": str(self.state_dir), "commands": self.commands, "limits": self.limits,
                "authorization": "none; profile inspection does not create grants or start work"}
