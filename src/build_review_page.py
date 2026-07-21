#!/usr/bin/env python3
"""Build a dependency-free local review workstation from enriched shot YAML.

The generated HTML can be opened directly through ``file://``. Video, frame and
contact-sheet paths in the YAML should be relative to the generated HTML file.
Review state is stored in the browser and can be exported as JSON.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("review_page_template.html")
APP_SCRIPT = Path(__file__).with_name("review_app_v4.js")


REQUIRED_FIELDS = {
    "shot_id",
    "title",
    "source_in_frame",
    "source_out_frame",
    "source_in",
    "source_out",
    "duration_sec",
    "phase",
    "boundary_type",
    "analysis_confidence",
    "start_frame",
    "middle_frame",
    "end_frame",
    "dense_contact_sheet",
    "clip",
}


def safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def load_payload(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    shots = payload.get("shots", []) if isinstance(payload, dict) else payload
    if not isinstance(shots, list) or not shots:
        raise SystemExit("shot YAML must contain a non-empty `shots` list")
    for index, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            raise SystemExit(f"shot #{index} must be an object")
        missing = sorted(REQUIRED_FIELDS - set(shot))
        if missing:
            raise SystemExit(f"{shot.get('shot_id', f'shot #{index}')} missing fields: {', '.join(missing)}")
    project = payload.get("project", {}) if isinstance(payload, dict) else {}
    if not isinstance(project, dict):
        project = {}
    project.setdefault("project_id", path.stem)
    project.setdefault("title", path.stem)
    project.setdefault("workflow_version", "legacy")
    project.setdefault("shot_count", len(shots))
    return project, shots


def load_shots(path: Path) -> list[dict[str, Any]]:
    """Backward-compatible shot loader used by existing integrations."""

    return load_payload(path)[1]


def render_document(shots: list[dict[str, Any]], project: dict[str, Any] | None = None) -> str:
    """Render a self-contained file:// compatible review workstation."""

    if not TEMPLATE.exists():
        raise SystemExit(f"missing template: {TEMPLATE}")
    if not APP_SCRIPT.exists():
        raise SystemExit(f"missing application script: {APP_SCRIPT}")
    document = TEMPLATE.read_text(encoding="utf-8")
    markers = ("__PROJECT_DATA__", "__SHOT_DATA__", "__APP_JS__")
    if any(document.count(marker) != 1 for marker in markers):
        raise SystemExit("review template must contain one project-data, shot-data and app-script marker")
    return (
        document.replace("__PROJECT_DATA__", safe_json(project or {}))
        .replace("__SHOT_DATA__", safe_json(shots))
        .replace("__APP_JS__", APP_SCRIPT.read_text(encoding="utf-8"))
    )


def build(shots_path: Path, output_path: Path) -> Path:
    project, shots = load_payload(shots_path)
    document = render_document(shots, project)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shots", type=Path, help="Enriched shot YAML")
    parser.add_argument("--output", type=Path, required=True, help="Generated HTML path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = build(args.shots.expanduser().resolve(), args.output.expanduser().resolve())
    print(f"Built review workstation: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
