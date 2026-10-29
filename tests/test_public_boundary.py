from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATTERNS = {
    "macOS user home": re.compile(r"/Users/[^/\s\"']+"),
    "Linux user home": re.compile(r"/home/[^/\s\"']+"),
    "Windows user home": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+"),
    "private reference id": re.compile(r"reference_[0-9a-f]{8,}", re.IGNORECASE),
}
EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "node_modules", "outputs", "__pycache__"}
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}


def public_files() -> list[Path]:
    """Files that would be published: tracked + untracked-but-not-ignored.

    Falls back to a filtered tree walk when git is unavailable (e.g. a source
    tarball). Local virtualenvs, node_modules and generated outputs are never
    part of the public tree, so they are always skipped.
    """

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        candidates = [ROOT / name for name in result.stdout.decode("utf-8").split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(ROOT.rglob("*"))
    return [
        path
        for path in candidates
        if path.is_file() and not EXCLUDED_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


class PublicBoundaryTests(unittest.TestCase):
    def test_public_tree_has_no_private_identifiers(self) -> None:
        violations: list[str] = []
        for path in public_files():
            if path.resolve() == Path(__file__).resolve():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Makefile", "VERSION", "NOTICE"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(text):
                    violations.append(f"{path.relative_to(ROOT)}: {label}")
        self.assertEqual(violations, [])

    def test_public_governance_and_ignored_data_boundaries_exist(self) -> None:
        for name in ("LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md"):
            self.assertTrue((ROOT / name).is_file(), name)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("outputs/*", ".env.*", "private/", "studio/"):
            self.assertIn(pattern, ignore)


if __name__ == "__main__":
    unittest.main()
