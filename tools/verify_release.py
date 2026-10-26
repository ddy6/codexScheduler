"""Build, inspect, and test local release artifacts in fresh virtual environments."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import time
import zipfile


FORBIDDEN_PARTS = {
    ".git", ".venv", "__pycache__", "state", "runs", "private", "scratch",
    "future_dev", "dev_notes", ".codex", ".agents", ".codex-scheduler",
}
FORBIDDEN_NAMES = {"AGENTS.md", "AGENTS.local.md", "AGENTS.override.md", "dev_plan.md", "dev_notes.md", ".env"}
DIST_INFO_FILES = {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE"}
EGG_INFO_FILES = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt", "entry_points.txt", "top_level.txt"}


def check_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or set(path.parts) & FORBIDDEN_PARTS
            or path.name in FORBIDDEN_NAMES or path.name.startswith(".env.")
            or path.suffix in {".log", ".sqlite", ".sqlite3", ".pyc", ".pyo"}
            or any(path.parts[i:i + 2] in {("docs", "dev"), ("docs", "internal"), ("docs", "guidance")}
                   for i in range(len(path.parts) - 1))):
        raise ValueError("Private or unsafe artifact path: " + name)
    return path


def inspect_artifact(path, public_files, private_values):
    """Match distributed source to public inputs; inspect generated metadata too."""
    wheel = path.suffix == ".whl"
    files = {}
    if wheel:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                check_path(info.filename)
                if info.file_size > 1048576:
                    raise ValueError("Oversized artifact member")
                if not info.is_dir():
                    if info.filename in files:
                        raise ValueError("Duplicate archive member")
                    files[info.filename] = archive.read(info)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                check_path(member.name)
                if member.issym() or member.islnk() or (not member.isfile() and not member.isdir()):
                    raise ValueError("Unexpected source archive member type")
                if member.size > 1048576:
                    raise ValueError("Oversized artifact member")
                if member.isfile():
                    relative = str(PurePosixPath(member.name).relative_to(PurePosixPath(member.name).parts[0]))
                    if relative in files:
                        raise ValueError("Duplicate archive member")
                    files[relative] = archive.extractfile(member).read()
    if sum(map(len, files.values())) > 10485760:
        raise ValueError("Artifact exceeded its review size bound")
    for name, data in files.items():
        check_path(name)
        if any(value and value in data for value in private_values):
            raise ValueError("Local runtime data found in artifact: " + name)
        if re.search(rb"sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}", data):
            raise ValueError("Credential-like content found in artifact: " + name)
        source_name = name
        generated = False
        if wheel:
            parts = PurePosixPath(name).parts
            if parts[0].endswith(".dist-info"):
                generated = True
                if "/".join(parts[1:]) not in DIST_INFO_FILES:
                    raise ValueError("Unexpected wheel metadata member: " + name)
                if parts[1:] == ("licenses", "LICENSE"):
                    source_name = "LICENSE"
                    generated = False
            elif parts[0].endswith(".data"):
                if parts[1:4] != ("data", "share", "codex-scheduler"):
                    raise ValueError("Unexpected wheel data location")
                source_name = "/".join(parts[4:])
            elif parts[0] != "codex_scheduler":
                raise ValueError("Unexpected installed package: " + name)
        elif PurePosixPath(name).parts[0].endswith(".egg-info"):
            if "/".join(PurePosixPath(name).parts[1:]) not in EGG_INFO_FILES:
                raise ValueError("Unexpected source metadata member: " + name)
            generated = True
        elif name in {"PKG-INFO", "setup.cfg"}:
            generated = True
        if not generated and (source_name not in public_files or public_files[source_name] != data):
            raise ValueError("Artifact contains an unreviewed or changed source file: " + name)
    required = {"codex_scheduler/__main__.py", "codex_scheduler/assets.py"}
    if not required <= files.keys():
        raise ValueError("Artifact is missing runtime modules")
    if wheel:
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in files):
            raise ValueError("Wheel is missing the license")
        if not any(name.endswith("/skills/bounded-workflow/SKILL.md") for name in files):
            raise ValueError("Wheel is missing the skill")
    elif not {"LICENSE", "pyproject.toml", "skills/bounded-workflow/SKILL.md", "examples/project.toml"} <= files.keys():
        raise ValueError("Source archive is missing setup assets")
    return {"file": path.name, "bytes": path.stat().st_size, "members": len(files),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class Verification:
    def __init__(self, source, work, python, artifacts=None):
        self.source = source.resolve()
        self.work = work.resolve()
        self.python = python
        self.artifact_source = artifacts.resolve() if artifacts else None
        if self.work == self.source or any(self.work.is_relative_to(self.source / folder)
                                          for folder in ("codex_scheduler", "skills", "tests", "tools", "docs", "examples", "prototypes")):
            raise ValueError("Verification work must stay outside distributed source directories")
        self.work.mkdir(parents=True, exist_ok=False)
        self.report = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                       "build_python": sys.version.split()[0], "steps": []}
        self.environment = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME", "CODEX_SCHEDULER_ROOT"):
            self.environment.pop(key, None)
        self.save()

    def save(self):
        temporary = self.work / "report.tmp"
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(self.work / "report.json")

    def command(self, label, argv, *, cwd=None, expected=0, timeout=120):
        print(label, flush=True)
        started = time.monotonic()
        result = subprocess.run(argv, cwd=cwd or self.work, env=self.environment,
                                capture_output=True, text=True, timeout=timeout)
        (self.work / (label + ".log")).write_text(result.stdout + result.stderr)
        self.report["steps"].append({"step": label, "exit_code": result.returncode,
                                     "seconds": round(time.monotonic() - started, 3)})
        self.save()
        if result.returncode != expected:
            raise RuntimeError(f"{label} failed; inspect its private log")
        return result.stdout

    def smoke(self, wheel):
        base = self.work / "base-env"
        self.command("create-base-env", [self.python, "-m", "venv", str(base)])
        python = base / "bin/python"
        cli = base / "bin/codex-scheduler"
        self.command("install-base-wheel", [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)])
        self.command("installed-import", [str(python), "-I", "-c",
            "from pathlib import Path; import sys, codex_scheduler, importlib.util; "
            "assert Path(codex_scheduler.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); "
            "assert importlib.util.find_spec('websockets') is None"])
        self.report["install_python"] = self.command("installed-python", [str(python), "-I", "-c",
            "import platform; print(platform.python_version())"]).strip()
        self.report["installed_version"] = self.command("installed-version", [str(cli), "--version"]).strip()
        project = self.work / "unrelated project"
        project.mkdir()
        (project / "tests").mkdir()
        (project / "tests/test_sample.py").write_text(
            "import unittest\nclass Sample(unittest.TestCase):\n"
            "    def test_sum(self):\n        self.assertEqual(sum(range(10)), 45)\n"
        )
        profile = project / "scheduler.toml"
        skill = self.work / "exported-skill"
        self.command("export-profile", [str(cli), "export-profile", str(profile)], cwd=project)
        self.command("export-skill", [str(cli), "export-skill", str(skill)], cwd=project)
        self.command("refuse-profile-overwrite", [str(cli), "export-profile", str(profile)], cwd=project, expected=2)
        self.command("refuse-skill-overwrite", [str(cli), "export-skill", str(skill)], cwd=project, expected=2)
        prefix = [str(python), "-I", str(skill / "scripts/scheduler.py"), "--", "--project", str(profile)]
        info = json.loads(self.command("installed-skill-helper", [*prefix, "project-info"], cwd=project))
        if Path(info["state_dir"]).exists():
            raise RuntimeError("Profile inspection unexpectedly created local state")
        grant = json.loads(self.command("create-offline-grant", [*prefix, "create-grant", "--thread", "offline-fixture",
                                      "--scope", "One offline release fixture; never contact a worker"], cwd=project))
        job = json.loads(self.command("register-offline-job", [*prefix, "register", "--grant", grant["grant_id"], "--command", "verify"], cwd=project))
        self.command("launch-offline-job", [*prefix, "launch", "--job", job["job_id"]], cwd=project)
        deadline = time.monotonic() + 10
        status = None
        for attempt in range(50):
            status = json.loads(self.command(f"inspect-offline-job-{attempt}", [*prefix, "status", "--job", job["job_id"]], cwd=project))
            if status["observed_state"] == status["recorded_state"] == "succeeded":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Offline fixture exceeded its inspection allowance")
            time.sleep(0.05)
        else:
            raise RuntimeError("Offline fixture did not complete")
        self.command("refuse-job-relaunch", [*prefix, "launch", "--job", job["job_id"]], cwd=project, expected=2)
        self.command("configure-offline-stop-fixture", [*prefix, "configure-delivery", "--grant", grant["grant_id"],
                     "--socket", str(self.work / "never-connected.sock"), "--max-checks", "5"], cwd=project)
        self.command("arm-offline-stop-fixture", [*prefix, "arm", "--job", job["job_id"],
                     "--instructions", "Offline fixture: this event must be revoked before dispatch"], cwd=project)
        self.command("revoke-offline-stop-fixture", [*prefix, "revoke-delivery", "--grant", grant["grant_id"]], cwd=project)
        delivery = json.loads(self.command("verify-no-dispatch", [*prefix, "deliver", "--job", job["job_id"]], cwd=project))
        policy = json.loads(self.command("verify-no-consumed-continuation", [*prefix, "delivery-policy", "--grant", grant["grant_id"]], cwd=project))
        if delivery["state"] != "revoked" or policy["consumed_continuations"] or policy["consumed_checks"]:
            raise RuntimeError("Revoked fixture attempted worker contact")
        self.report["offline_fixture"] = {"jobs": 1, "outcome": status["observed_state"],
                                          "model_calls": 0, "continuations": 0, "revocation": "passed"}
        wheelhouse = self.work / "wheelhouse"
        self.command("download-optional-transport", [str(python), "-I", "-m", "pip", "download", "--only-binary=:all:",
                     "--no-deps", "--dest", str(wheelhouse), "websockets>=15,<18"])
        optional = self.work / "codex-env"
        self.command("create-codex-env", [self.python, "-m", "venv", str(optional)])
        optional_python = optional / "bin/python"
        self.command("install-codex-extra", [str(optional_python), "-I", "-m", "pip", "install", "--no-index",
                     "--find-links", str(wheelhouse), str(wheel) + "[codex]"])
        self.report["optional_transport_version"] = self.command("check-codex-extra", [str(optional_python), "-I", "-c",
            "from importlib.metadata import version; from codex_scheduler.codex import CodexAdapter; "
            "assert callable(CodexAdapter.require_available()); print(version('websockets'))"]).strip()

    def run(self):
        public_names = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                                      cwd=self.source, capture_output=True, text=True, check=True).stdout.splitlines()
        public = {name: (self.source / name).read_bytes() for name in public_names if (self.source / name).is_file()}
        private = [str(path).encode() for path in (self.source, self.work, Path.home())]
        for status_path in (self.source / "runs").glob("*/status.json"):
            if status_path.stat().st_size <= 65536:
                saved = json.loads(status_path.read_text())
                private.extend(value.encode() for key, value in saved.items()
                               if key.endswith("_id") and isinstance(value, str) and len(value) >= 16)
                token = re.search(r"context token ([a-f0-9]+)", saved.get("initial_prompt", ""))
                if token:
                    private.append(token.group(1).encode())
        artifacts = self.artifact_source or self.work / "artifacts"
        if self.artifact_source is None:
            self.command("build-sdist-and-wheel", [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)], cwd=self.source)
        wheels = list(artifacts.glob("*.whl"))
        sources = list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sources) != 1:
            raise RuntimeError("Expected one wheel and one source archive")
        self.report["artifacts"] = [inspect_artifact(path, public, private) for path in [*wheels, *sources]]
        self.save()
        self.smoke(wheels[0])
        self.report["status"] = "passed"
        self.report["completed_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        print("Release verification passed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="New ignored directory; existing directories are refused")
    parser.add_argument("--python", default=sys.executable, help="Interpreter for fresh install environments")
    parser.add_argument("--artifacts", type=Path, help="Reuse an existing wheel/source pair; still audit against current source")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Release verification currently requires a POSIX host")
    verification = Verification(Path(__file__).resolve().parent.parent, args.work_dir, args.python, args.artifacts)
    try:
        verification.run()
    except Exception as error:
        verification.report.update(status="failed", error=str(error))
        verification.save()
        print("Release verification stopped; inspect report.json and private logs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
