from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from local_settings import LocalSettingsStore, normalize_projects_root, resolve_projects_root  # noqa: E402


class LocalSettingsTests(unittest.TestCase):
    def test_projects_root_is_persisted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = LocalSettingsStore(root / "config" / "settings.json")
            projects = root / "Library" / "Projects"

            saved = settings.save_projects_root(projects)

            self.assertEqual(saved, projects.resolve())
            self.assertEqual(settings.projects_root(), projects.resolve())
            self.assertTrue(projects.is_dir())
            self.assertFalse(settings.path.with_suffix(".json.tmp").exists())

    def test_projects_root_precedence_is_cli_then_saved_then_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "cli"
            saved = root / "saved"
            environment = root / "environment"
            settings = LocalSettingsStore(root / "settings.json")
            settings.save_projects_root(saved)

            self.assertEqual(
                resolve_projects_root(cli_root=cli, settings=settings, env_root=str(environment)),
                cli.resolve(),
            )
            self.assertEqual(
                resolve_projects_root(settings=settings, env_root=str(environment)),
                saved.resolve(),
            )

            empty_settings = LocalSettingsStore(root / "empty-settings.json")
            self.assertEqual(
                resolve_projects_root(settings=empty_settings, env_root=str(environment)),
                environment.resolve(),
            )

    def test_projects_root_rejects_unsafe_or_ambiguous_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "not-a-directory"
            file_path.write_text("file", encoding="utf-8")

            with self.assertRaises(ValueError):
                normalize_projects_root("", require_absolute=True)
            with self.assertRaises(ValueError):
                normalize_projects_root("relative/projects", require_absolute=True)
            with self.assertRaises(ValueError):
                normalize_projects_root(Path(root.anchor), require_absolute=True)
            with self.assertRaises(ValueError):
                normalize_projects_root(file_path, require_absolute=True)


if __name__ == "__main__":
    unittest.main()
