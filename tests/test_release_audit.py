"""Archive review rejects private inputs and ambiguous members before release."""

from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from tools.verify_release import inspect_artifact


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.public = {
            "codex_scheduler/__main__.py": b"fixture main\n",
            "codex_scheduler/assets.py": b"fixture assets\n",
            "skills/bounded-workflow/SKILL.md": b"fixture skill\n",
            "LICENSE": b"fixture license\n",
        }
        self.entries = {
            "codex_scheduler/__main__.py": self.public["codex_scheduler/__main__.py"],
            "codex_scheduler/assets.py": self.public["codex_scheduler/assets.py"],
            "fixture.data/data/share/codex-scheduler/skills/bounded-workflow/SKILL.md": self.public["skills/bounded-workflow/SKILL.md"],
            "fixture.dist-info/licenses/LICENSE": self.public["LICENSE"],
        }

    def wheel(self, extra=None):
        path = self.root / "fixture.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in {**self.entries, **(extra or {})}.items():
                archive.writestr(name, data)
        return path

    def test_review_accepts_only_matching_public_inputs_and_expected_metadata(self):
        result = inspect_artifact(self.wheel({"fixture.dist-info/METADATA": b"public metadata"}), self.public, [])
        self.assertEqual(result["members"], 5)
        self.assertEqual(len(result["sha256"]), 64)

    def test_private_guidance_state_and_unsafe_paths_are_rejected(self):
        for name in ("dev_plan.md", "AGENTS.md", "state/result.json", "runs/result.json", ".env.local",
                     "docs/internal/note.md", "../outside.py", "/absolute.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_artifact(self.wheel({name: b"must stay private"}), self.public, [])

    def test_ignored_or_changed_code_is_not_a_public_build_input(self):
        for name in ("codex_scheduler/local_probe.py", "codex_scheduler/__main__.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_artifact(self.wheel({name: b"unreviewed code"}), self.public, [])

    def test_license_must_match_the_selected_public_notice(self):
        with self.assertRaises(ValueError):
            inspect_artifact(self.wheel({"fixture.dist-info/licenses/LICENSE": b"wrong notice"}), self.public, [])

    def test_local_identifiers_are_rejected_even_inside_generated_metadata(self):
        with self.assertRaises(ValueError):
            inspect_artifact(self.wheel({"fixture.dist-info/METADATA": b"sensitive-fixture-marker"}),
                             self.public, [b"sensitive-fixture-marker"])

    def test_source_archive_links_are_rejected_without_extracting_them(self):
        path = self.root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            link = tarfile.TarInfo("fixture/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            archive.addfile(link)
        with self.assertRaises(ValueError):
            inspect_artifact(path, self.public, [])
        self.assertFalse((self.root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
