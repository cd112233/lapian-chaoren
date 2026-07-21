from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATTERNS = {
    "macOS user home": re.compile(r"/Users/[^/\s\"']+"),
    "Linux user home": re.compile(r"/home/[^/\s\"']+"),
    "Windows user home": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+"),
    "private reference id": re.compile(r"reference_[0-9a-f]{8,}", re.IGNORECASE),
}
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}


class PublicBoundaryTests(unittest.TestCase):
    def test_public_tree_has_no_private_identifiers(self) -> None:
        violations: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.resolve() == Path(__file__).resolve():
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
