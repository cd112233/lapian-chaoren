#!/usr/bin/env python3
"""Build the self-contained shell for a local remake comparison workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


TEMPLATE = Path(__file__).with_name("comparison_page_template.html")
APP_SCRIPT = Path(__file__).with_name("comparison_app.js")


def safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_document(comparison: dict[str, Any]) -> str:
    if not TEMPLATE.is_file():
        raise SystemExit(f"missing template: {TEMPLATE}")
    if not APP_SCRIPT.is_file():
        raise SystemExit(f"missing application script: {APP_SCRIPT}")
    document = TEMPLATE.read_text(encoding="utf-8")
    markers = ("__COMPARISON_DATA__", "__COMPARISON_APP_JS__")
    if any(document.count(marker) != 1 for marker in markers):
        raise SystemExit("comparison template markers are invalid")
    return (
        document.replace("__COMPARISON_DATA__", safe_json(comparison))
        .replace("__COMPARISON_APP_JS__", APP_SCRIPT.read_text(encoding="utf-8"))
    )


def build(manifest_path: Path, output_path: Path) -> Path:
    comparison = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(comparison, dict) or not comparison.get("comparison_id"):
        raise SystemExit("comparison manifest is invalid")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_document(comparison), encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = build(args.manifest.expanduser().resolve(), args.output.expanduser().resolve())
    print(f"Built comparison workspace: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
