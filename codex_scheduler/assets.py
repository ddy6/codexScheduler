"""Export bundled setup assets without starting work or overwriting user files."""

from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
import tomllib

from .registry import SchedulerError


SKILL_FILES = (
    "SKILL.md", "agents/openai.yaml", "references/usage.md", "scripts/scheduler.py",
)


def package_version():
    try:
        return version("codex-scheduler")
    except PackageNotFoundError:
        return "source checkout"


def bundled_bytes(relative):
    relative = Path(relative)
    root = Path(__file__).resolve().parent.parent
    marker = root / "pyproject.toml"
    if marker.is_file():
        with marker.open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
        if project.get("name") == "codex-scheduler":
            return (root / relative).read_bytes()
    try:
        installed = distribution("codex-scheduler")
    except PackageNotFoundError as error:
        raise SchedulerError("Bundled resources require an installed package or source checkout") from error
    suffix = ("share", "codex-scheduler", *relative.parts)
    matches = [entry for entry in installed.files or ()
               if tuple(entry.parts[-len(suffix):]) == suffix]
    if len(matches) != 1:
        raise SchedulerError("Installed package is missing a unique bundled resource")
    return installed.locate_file(matches[0]).read_bytes()


def export_skill(destination):
    # Read the entire bundle before creating a destination.
    files = {name: bundled_bytes(Path("skills/bounded-workflow") / name) for name in SKILL_FILES}
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    return {"destination": str(destination), "files": list(files)}


def export_profile(destination):
    data = bundled_bytes("examples/project.toml")
    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(data)
    return {"destination": str(destination)}
