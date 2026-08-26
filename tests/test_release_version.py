from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_release_version import validate_release_version


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "room_alignment").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_project(self, package: str = "1.2.3", runtime: str = "1.2.3") -> None:
        (self.root / "pyproject.toml").write_text(
            f'[project]\nname = "room-alignment"\nversion = "{package}"\n',
            encoding="utf-8",
        )
        (self.root / "room_alignment" / "__init__.py").write_text(
            f'__version__ = "{runtime}"\n', encoding="utf-8"
        )
        (self.root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {package} — Release\n", encoding="utf-8"
        )

    def test_accepts_matching_package_runtime_changelog_and_tag(self) -> None:
        self.write_project()
        self.assertEqual(validate_release_version(self.root, "v1.2.3"), "1.2.3")

    def test_rejects_runtime_version_drift(self) -> None:
        self.write_project(runtime="1.2.4")
        with self.assertRaisesRegex(ValueError, "Version mismatch"):
            validate_release_version(self.root)

    def test_rejects_release_tag_drift(self) -> None:
        self.write_project()
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_release_version(self.root, "v1.2.4")


if __name__ == "__main__":
    unittest.main()
