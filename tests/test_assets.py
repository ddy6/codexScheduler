"""Bundled setup files are complete, inert, and never overwrite a destination."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_scheduler.__main__ import main as cli
from codex_scheduler.assets import SKILL_FILES, bundled_bytes, export_profile, export_skill
from codex_scheduler.project import Project


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_exported_skill_contains_exactly_the_bundled_resources(self):
        destination = self.root / "skill"
        export_skill(destination)
        files = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(files, set(SKILL_FILES))
        for name in SKILL_FILES:
            self.assertEqual((destination / name).read_bytes(), bundled_bytes(Path("skills/bounded-workflow") / name))

    def test_exported_profile_is_valid_and_does_not_create_a_registry(self):
        path = self.root / "scheduler.toml"
        export_profile(path)
        profile = Project.load(path)
        self.assertEqual(profile.workspace, self.root.resolve())
        self.assertFalse(profile.state_dir.exists())

    def test_existing_destinations_are_preserved(self):
        profile = self.root / "scheduler.toml"
        profile.write_text("user profile")
        with self.assertRaises(FileExistsError):
            export_profile(profile)
        self.assertEqual(profile.read_text(), "user profile")
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "user.txt").write_text("keep")
        with self.assertRaises(FileExistsError):
            export_skill(skill)
        self.assertEqual((skill / "user.txt").read_text(), "keep")

    def test_dangling_symlinks_are_existing_destinations_too(self):
        target = self.root / "missing-target"
        profile = self.root / "scheduler.toml"
        profile.symlink_to(target)
        with self.assertRaises(FileExistsError):
            export_profile(profile)
        self.assertFalse(target.exists())
        skill = self.root / "skill"
        skill.symlink_to(target, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            export_skill(skill)
        self.assertFalse(target.exists())

    def test_cli_export_does_not_initialize_job_state(self):
        with patch("codex_scheduler.__main__.Registry") as registry, redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["export-profile", str(self.root / "scheduler.toml")]), 0)
            self.assertEqual(cli(["export-skill", str(self.root / "skill")]), 0)
            registry.assert_not_called()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["export-profile", str(self.root / "scheduler.toml")]), 2)


if __name__ == "__main__":
    unittest.main()
