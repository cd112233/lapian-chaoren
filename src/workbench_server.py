#!/usr/bin/env python3
"""Local project library and video-ingest server for Lapian Chaoren.

The server is deliberately local-only by default. It receives a video from the
browser, runs the existing mechanical analysis, builds the learning scaffold,
and exposes the generated review page without uploading media to a third party.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from build_review_page import build as build_review_page
from learning_project import WORKFLOW_VERSION, build_learning_yaml


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_HTML = Path(__file__).with_name("workbench.html")
DEFAULT_PROJECTS_ROOT = ROOT / "outputs" / "projects"
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
CHUNK_SIZE = 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(raw: str) -> str:
    name = Path(unquote(raw or "reference.mp4")).name.strip()
    name = re.sub(r"[^\w.()\- ]+", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        name = "reference.mp4"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise ValueError(f"unsupported video format: {suffix or 'missing extension'}")
    stem = name[: -len(suffix)].rstrip(" .") or "reference"
    stem = stem[: 180 - len(suffix)].rstrip(" .") or "reference"
    return f"{stem}{suffix}"


def safe_title(raw: str, filename: str) -> str:
    value = re.sub(r"[\x00-\x1f]+", " ", unquote(raw or "")).strip()
    return (value or Path(filename).stem)[:120]


def new_project_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def project_root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", project_id):
            raise ValueError("invalid project id")
        path = (self.root / project_id).resolve()
        path.relative_to(self.root)
        return path

    def manifest_path(self, project_id: str) -> Path:
        return self.project_root(project_id) / "project.json"

    def read(self, project_id: str) -> dict[str, Any]:
        path = self.manifest_path(project_id)
        if not path.is_file():
            raise FileNotFoundError(project_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, project_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = self.manifest_path(project_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            manifest["updated_at"] = utc_now()
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        return manifest

    def update(self, project_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            manifest = self.read(project_id)
            manifest.update(changes)
            return self.write(project_id, manifest)

    def list(self) -> list[dict[str, Any]]:
        projects = []
        for path in self.root.glob("*/project.json"):
            try:
                projects.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(projects, key=lambda item: item.get("created_at", ""), reverse=True)


def write_upload(
    stream: BinaryIO,
    target: Path,
    content_length: int,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".uploading")
    digest = hashlib.sha256()
    remaining = content_length
    try:
        with temporary.open("wb") as handle:
            while remaining:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise ConnectionError("upload ended before Content-Length bytes were received")
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def process_project(store: ProjectStore, project_id: str) -> None:
    manifest = store.read(project_id)
    project_root = store.project_root(project_id)
    source_path = project_root / manifest["source_relpath"]
    analysis_root = project_root / "analysis"
    try:
        store.update(
            project_id,
            status="analyzing",
            stage="正在检测切点、抽帧并生成逐镜片段",
            progress=18,
            error=None,
        )
        command = [
            sys.executable,
            str(ROOT / "src" / "analyze_reference.py"),
            str(source_path),
            "--output",
            str(analysis_root),
            "--detector",
            "adaptive",
            "--split-clips",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "analysis failed").strip()
            raise RuntimeError(detail[-4000:])

        store.update(
            project_id,
            status="building",
            stage="正在建立人机学习记录和审片工作台",
            progress=82,
        )
        learning_yaml = analysis_root / "learning_shots.yaml"
        build_learning_yaml(
            analysis_root,
            learning_yaml,
            project_id=project_id,
            title=manifest["title"],
            source_path=source_path,
            source_fingerprint=manifest["source_fingerprint"],
        )
        review_path = analysis_root / "review.html"
        build_review_page(learning_yaml, review_path)
        metadata = json.loads((analysis_root / "metadata.json").read_text(encoding="utf-8"))
        probe = metadata.get("probe", {})
        detection = metadata.get("detection", {})
        store.update(
            project_id,
            status="ready",
            stage="可以开始人机审片",
            progress=100,
            shot_count=int(detection.get("shot_count") or 0),
            duration_sec=float(probe.get("duration_sec") or 0),
            fps=float(probe.get("fps") or 0),
            review_relpath="analysis/review.html",
            learning_yaml_relpath="analysis/learning_shots.yaml",
            completed_at=utc_now(),
        )
    except Exception as error:
        store.update(
            project_id,
            status="failed",
            stage="处理失败",
            progress=0,
            error=str(error)[:4000],
        )


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    store: ProjectStore
    max_upload_bytes: int


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send_bytes(
            status,
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "file not found"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        cache = "public, max-age=3600" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4"} else "no-store"
        size = path.stat().st_size
        if size == 0:
            self._send_bytes(HTTPStatus.OK, b"", content_type, cache=cache)
            return
        start, end = 0, max(0, size - 1)
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:
                suffix_length = int(match.group(2))
                start = max(0, size - suffix_length)
            end = min(end, size - 1)
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._serve_file(WORKBENCH_HTML)
            return
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "lapian-local-workbench",
                    "workflow_version": WORKFLOW_VERSION,
                    "projects_root": str(self.server.store.root),
                },
            )
            return
        if path == "/api/projects":
            self._send_json(HTTPStatus.OK, {"projects": self.server.store.list()})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            try:
                manifest = self.server.store.read(match.group(1))
            except (ValueError, FileNotFoundError):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "project not found"})
                return
            self._send_json(HTTPStatus.OK, {"project": manifest})
            return
        match = re.fullmatch(r"/projects/([^/]+)/(.*)", path)
        if match:
            try:
                root = self.server.store.project_root(match.group(1))
                target = (root / unquote(match.group(2))).resolve()
                target.relative_to(root)
            except (ValueError, FileNotFoundError):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "project file not found"})
                return
            self._serve_file(target)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/projects":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "video body is required"})
            return
        if content_length > self.server.max_upload_bytes:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "video exceeds local upload limit"})
            return
        try:
            filename = safe_filename(self.headers.get("X-Filename", ""))
            title = safe_title(self.headers.get("X-Project-Title", ""), filename)
        except ValueError as error:
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": str(error)})
            return

        project_id = new_project_id()
        root = self.server.store.project_root(project_id)
        source_path = root / "source" / filename
        created_at = utc_now()
        manifest = {
            "project_id": project_id,
            "title": title,
            "source_filename": filename,
            "source_relpath": source_path.relative_to(root).as_posix(),
            "source_size": content_length,
            "source_fingerprint": "",
            "status": "uploading",
            "stage": "正在保存本地片源",
            "progress": 0,
            "workflow_version": WORKFLOW_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
            "error": None,
        }
        self.server.store.write(project_id, manifest)
        try:
            fingerprint = write_upload(self.rfile, source_path, content_length)
            manifest = self.server.store.update(
                project_id,
                source_fingerprint=fingerprint,
                status="queued",
                stage="已进入本地分析队列",
                progress=8,
            )
        except Exception as error:
            self.server.store.update(
                project_id,
                status="failed",
                stage="片源保存失败",
                progress=0,
                error=str(error)[:2000],
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error), "project_id": project_id})
            return

        worker = threading.Thread(
            target=process_project,
            args=(self.server.store, project_id),
            daemon=True,
            name=f"lapian-{project_id}",
        )
        worker.start()
        self._send_json(HTTPStatus.ACCEPTED, {"project": manifest})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("LAPIAN_WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LAPIAN_WORKBENCH_PORT", "8765")))
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=Path(os.getenv("LAPIAN_PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT)),
    )
    parser.add_argument(
        "--max-upload-gb",
        type=float,
        default=float(os.getenv("LAPIAN_MAX_UPLOAD_GB", "20")),
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the project library in a browser")
    return parser.parse_args()


def serve(
    *,
    host: str,
    port: int,
    projects_root: Path,
    max_upload_bytes: int,
    open_browser: bool,
) -> None:
    server = WorkbenchHTTPServer((host, port), WorkbenchHandler)
    server.store = ProjectStore(projects_root)
    server.max_upload_bytes = max_upload_bytes
    url = f"http://{host}:{server.server_address[1]}"
    print(f"Lapian local workbench: {url}")
    print(f"Projects root: {server.store.root}")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLapian local workbench stopped.")
    finally:
        server.server_close()


def main() -> int:
    args = parse_args()
    max_upload_bytes = max(1, int(args.max_upload_gb * 1024 * 1024 * 1024))
    serve(
        host=args.host,
        port=args.port,
        projects_root=args.projects_root,
        max_upload_bytes=max_upload_bytes,
        open_browser=not args.no_open,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
