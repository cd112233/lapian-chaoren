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
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from build_review_page import build as build_review_page
from learning_project import WORKFLOW_VERSION, build_learning_yaml
from local_settings import (
    DEFAULT_PROJECTS_ROOT,
    LocalSettingsStore,
    normalize_projects_root,
    resolve_projects_root,
)
from video_adapter import VIDEO_SUFFIXES, adapt_video


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_HTML = Path(__file__).with_name("workbench.html")
ALLOWED_VIDEO_SUFFIXES = VIDEO_SUFFIXES
CHUNK_SIZE = 1024 * 1024
MAX_SETTINGS_BODY_BYTES = 16 * 1024
BUSY_PROJECT_STATUSES = {"uploading", "queued", "adapting", "analyzing", "building"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(raw: str, content_type: str = "") -> str:
    name = Path(unquote(raw or "reference.mp4")).name.strip()
    name = re.sub(r"[^\w.()\- ]+", "_", name, flags=re.UNICODE).strip(" .")
    if not name:
        name = "reference.mp4"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        mime = content_type.split(";", 1)[0].strip().lower()
        if not mime.startswith("video/"):
            raise ValueError(f"unsupported video format: {suffix or 'missing extension'}")
        if not suffix:
            name = f"{name[:170].rstrip(' .') or 'reference'}.input"
            suffix = ".input"
    stem = name[: -len(suffix)].rstrip(" .") or "reference"
    stem = stem[: 180 - len(suffix)].rstrip(" .") or "reference"
    return f"{stem}{suffix}"


def safe_title(raw: str, filename: str) -> str:
    value = re.sub(r"[\x00-\x1f]+", " ", unquote(raw or "")).strip()
    return (value or Path(filename).stem)[:120]


def new_project_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def select_directory(initial: Path) -> Path | None:
    initial = initial.expanduser().resolve()
    if sys.platform == "darwin":
        script = """
on run argv
  try
    set chosenFolder to choose folder with prompt "选择拉片项目保存位置" default location POSIX file (item 1 of argv)
    return POSIX path of chosenFolder
  on error number -128
    return ""
  end try
end run
""".strip()
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script, str(initial)],
            text=True,
            capture_output=True,
            check=False,
        )
    elif os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$dialog=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$dialog.SelectedPath=$args[0];"
            "if($dialog.ShowDialog() -eq 'OK'){Write-Output $dialog.SelectedPath}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script, str(initial)],
            text=True,
            capture_output=True,
            check=False,
        )
    elif shutil.which("zenity"):
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", f"--filename={initial}/"],
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        raise RuntimeError("系统文件夹选择器不可用，请手动输入绝对路径。")
    selected = result.stdout.strip()
    if not selected and result.returncode in {0, 1}:
        return None
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "无法打开系统文件夹选择器").strip()[:1000])
    return normalize_projects_root(selected, create=False, require_absolute=True)


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

    def delete(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self.read(project_id)
            root = self.project_root(project_id)
            shutil.rmtree(root)
        return manifest

    def recover_interrupted(self) -> int:
        recovered = 0
        for manifest in self.list():
            if manifest.get("status") not in BUSY_PROJECT_STATUSES:
                continue
            self.update(
                manifest["project_id"],
                status="failed",
                stage="上次处理被中断",
                progress_phase="interrupted",
                progress_detail="原片副本仍保留，可删除后重新导入",
                error="本地服务在分析完成前停止。",
            )
            recovered += 1
        return recovered


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


def parse_progress_event(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "progress":
        return None
    try:
        progress = max(0, min(100, int(payload["progress"])))
    except (KeyError, TypeError, ValueError):
        return None
    stage = str(payload.get("stage") or "正在分析").strip()[:160]
    detail = str(payload.get("detail") or "").strip()[:300]
    phase = re.sub(r"[^a-z0-9_-]+", "", str(payload.get("phase") or "analysis").lower())[:48]
    event: dict[str, Any] = {
        "progress": progress,
        "stage": stage,
        "progress_detail": detail,
        "progress_phase": phase or "analysis",
    }
    for key in ("current", "total"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            event[f"progress_{key}"] = value
    return event


def process_project(store: ProjectStore, project_id: str) -> None:
    manifest = store.read(project_id)
    project_root = store.project_root(project_id)
    source_path = project_root / manifest["source_relpath"]
    analysis_source_path = source_path
    analysis_root = project_root / "analysis"
    try:
        store.update(
            project_id,
            status="adapting",
            stage="正在检查视频格式",
            progress=9,
            progress_phase="format_check",
            progress_detail="读取容器、视频编码和音频轨道",
            progress_current=None,
            progress_total=None,
            error=None,
        )
        adaptation_output = project_root / "source" / "analysis_source.mp4"
        if adaptation_output.resolve() == source_path.resolve():
            adaptation_output = project_root / "source" / "analysis_source_adapted.mp4"
        last_adaptation_progress = -1

        def report_adaptation_progress(value: int) -> None:
            nonlocal last_adaptation_progress
            normalized = max(0, min(100, int(value)))
            if normalized == last_adaptation_progress:
                return
            last_adaptation_progress = normalized
            store.update(
                project_id,
                status="adapting",
                stage="正在自动适配视频格式",
                progress=9 + round(normalized * 3 / 100),
                progress_phase="adapting",
                progress_detail=f"本地适配进度 {normalized}%",
            )

        adaptation = adapt_video(
            source_path,
            adaptation_output,
            on_progress=report_adaptation_progress,
        )
        analysis_source_path = adaptation.analysis_path
        adaptation_info = adaptation.manifest()
        manifest = store.update(
            project_id,
            analysis_source_relpath=analysis_source_path.relative_to(project_root).as_posix(),
            format_adaptation=adaptation_info,
            status="analyzing",
            stage="视频格式已就绪",
            progress=12,
            progress_phase="format_ready",
            progress_detail=adaptation.reason,
        )
        command = [
            sys.executable,
            str(ROOT / "src" / "analyze_reference.py"),
            str(analysis_source_path),
            "--output",
            str(analysis_root),
            "--detector",
            "adaptive",
            "--split-clips",
            "--progress-json",
            "--progress-offset",
            "4",
        ]
        output_tail: deque[str] = deque(maxlen=100)
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            output_tail.append(line.rstrip())
            event = parse_progress_event(line)
            if event:
                store.update(project_id, status="analyzing", **event)
        return_code = process.wait()
        if return_code != 0:
            detail = "\n".join(output_tail).strip() or "analysis failed"
            raise RuntimeError(detail[-4000:])

        store.update(
            project_id,
            status="building",
            stage="正在建立人机学习记录和审片工作台",
            progress=93,
            progress_phase="learning",
            progress_detail="正在生成可审核的字段结构",
            progress_current=None,
            progress_total=None,
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
        store.update(
            project_id,
            status="building",
            stage="正在生成审片工作台",
            progress=97,
            progress_phase="review_page",
            progress_detail="正在组装逐镜证据与学习记录",
        )
        review_path = analysis_root / "review.html"
        build_review_page(learning_yaml, review_path)
        store.update(
            project_id,
            status="building",
            stage="正在完成项目索引",
            progress=99,
            progress_phase="finalizing",
            progress_detail="即将完成",
        )
        metadata = json.loads((analysis_root / "metadata.json").read_text(encoding="utf-8"))
        probe = metadata.get("probe", {})
        detection = metadata.get("detection", {})
        frame_width = int(probe.get("display_width") or probe.get("width") or 0)
        frame_height = int(probe.get("display_height") or probe.get("height") or 0)
        store.update(
            project_id,
            status="ready",
            stage="可以开始人机审片",
            progress=100,
            progress_phase="ready",
            progress_detail=f"已生成 {int(detection.get('shot_count') or 0)} 个镜头",
            progress_current=None,
            progress_total=None,
            shot_count=int(detection.get("shot_count") or 0),
            duration_sec=float(probe.get("duration_sec") or 0),
            fps=float(probe.get("fps") or 0),
            frame_width=frame_width,
            frame_height=frame_height,
            display_aspect_ratio=float(probe.get("display_aspect_ratio") or 0),
            aspect_ratio_label=str(probe.get("aspect_ratio_label") or ""),
            orientation=str(probe.get("orientation") or "landscape"),
            rotation_deg=int(probe.get("rotation_deg") or 0),
            review_relpath="analysis/review.html",
            learning_yaml_relpath="analysis/learning_shots.yaml",
            completed_at=utc_now(),
        )
    except Exception as error:
        try:
            store.update(
                project_id,
                status="failed",
                stage="处理失败",
                progress_phase="failed",
                progress_detail="分析未完成，请查看错误信息",
                error=str(error)[:4000],
            )
        except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError):
            pass


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    store: ProjectStore
    max_upload_bytes: int

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state_lock = threading.RLock()
        self._active_projects: set[str] = set()
        self.settings_store = LocalSettingsStore()
        self.settings_locked = False
        self.default_projects_root = DEFAULT_PROJECTS_ROOT.resolve()

    def current_store(self) -> ProjectStore:
        with self._state_lock:
            return self.store

    def reserve_project(self, project_id: str) -> ProjectStore:
        with self._state_lock:
            self._active_projects.add(project_id)
            return self.store

    def mark_project_active(self, project_id: str) -> None:
        with self._state_lock:
            self._active_projects.add(project_id)

    def mark_project_inactive(self, project_id: str) -> None:
        with self._state_lock:
            self._active_projects.discard(project_id)

    def project_is_active(self, project_id: str) -> bool:
        with self._state_lock:
            return project_id in self._active_projects

    def settings_payload(self) -> dict[str, Any]:
        with self._state_lock:
            store = self.store
            return {
                "projects_root": str(store.root),
                "default_projects_root": str(self.default_projects_root),
                "project_count": len(store.list()),
                "editable": not self.settings_locked,
                "busy": bool(self._active_projects),
            }

    def switch_projects_root(self, value: str) -> dict[str, Any]:
        with self._state_lock:
            if self.settings_locked:
                raise PermissionError("保存位置由启动参数锁定，无法在网页中修改。")
            if self._active_projects:
                raise RuntimeError("仍有项目正在上传或分析，完成后才能切换保存位置。")
            root = normalize_projects_root(value, create=True, require_absolute=True)
            store = ProjectStore(root)
            recovered = store.recover_interrupted()
            self.settings_store.save_projects_root(root)
            self.store = store
            payload = self.settings_payload()
            payload["recovered"] = recovered
            return payload

    def delete_project(self, project_id: str) -> dict[str, Any]:
        with self._state_lock:
            if project_id in self._active_projects:
                raise RuntimeError("项目仍在上传或分析，完成后才能删除。")
            return self.store.delete(project_id)


def run_project_worker(server: WorkbenchHTTPServer, store: ProjectStore, project_id: str) -> None:
    try:
        process_project(store, project_id)
    finally:
        server.mark_project_inactive(project_id)


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

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("无效的请求长度。") from error
        if content_length <= 0 or content_length > MAX_SETTINGS_BODY_BYTES:
            raise ValueError("设置请求为空或过大。")
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("设置请求不是有效 JSON。") from error
        if not isinstance(payload, dict):
            raise ValueError("设置请求必须是 JSON 对象。")
        return payload

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
            store = self.server.current_store()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "lapian-local-workbench",
                    "workflow_version": WORKFLOW_VERSION,
                    "projects_root": str(store.root),
                },
            )
            return
        if path == "/api/settings":
            self._send_json(HTTPStatus.OK, {"settings": self.server.settings_payload()})
            return
        if path == "/api/projects":
            self._send_json(HTTPStatus.OK, {"projects": self.server.current_store().list()})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            try:
                manifest = self.server.current_store().read(match.group(1))
            except (ValueError, FileNotFoundError):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "project not found"})
                return
            self._send_json(HTTPStatus.OK, {"project": manifest})
            return
        match = re.fullmatch(r"/projects/([^/]+)/(.*)", path)
        if match:
            try:
                root = self.server.current_store().project_root(match.group(1))
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
        if parsed.path == "/api/settings/select-projects-root":
            if self.server.settings_locked:
                self._send_json(HTTPStatus.CONFLICT, {"error": "保存位置由启动参数锁定。"})
                return
            try:
                selected = select_directory(self.server.current_store().root)
            except (ValueError, PermissionError, RuntimeError, OSError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)[:1000]})
                return
            self._send_json(
                HTTPStatus.OK,
                {"cancelled": selected is None, "path": str(selected) if selected else ""},
            )
            return
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
            content_type = self.headers.get("Content-Type", "")
            filename = safe_filename(self.headers.get("X-Filename", ""), content_type)
            title = safe_title(self.headers.get("X-Project-Title", ""), filename)
        except ValueError as error:
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": str(error)})
            return

        project_id = new_project_id()
        store = self.server.reserve_project(project_id)
        root: Path | None = None
        manifest_created = False
        try:
            root = store.project_root(project_id)
            source_path = root / "source" / filename
            created_at = utc_now()
            manifest = {
                "project_id": project_id,
                "title": title,
                "source_filename": filename,
                "source_relpath": source_path.relative_to(root).as_posix(),
                "analysis_source_relpath": source_path.relative_to(root).as_posix(),
                "source_size": content_length,
                "source_fingerprint": "",
                "status": "uploading",
                "stage": "正在保存本地片源",
                "progress": 0,
                "progress_phase": "uploading",
                "progress_detail": "正在接收本地片源",
                "progress_current": None,
                "progress_total": None,
                "workflow_version": WORKFLOW_VERSION,
                "created_at": created_at,
                "updated_at": created_at,
                "error": None,
                "format_adaptation": {"status": "pending", "preserved_original": True},
            }
            store.write(project_id, manifest)
            manifest_created = True
            fingerprint = write_upload(self.rfile, source_path, content_length)
            manifest = store.update(
                project_id,
                source_fingerprint=fingerprint,
                status="queued",
                stage="已进入本地分析队列",
                progress=8,
                progress_phase="queued",
                progress_detail="等待分析线程启动",
            )
        except Exception as error:
            self.server.mark_project_inactive(project_id)
            if manifest_created:
                try:
                    store.update(
                        project_id,
                        status="failed",
                        stage="片源保存失败",
                        progress=0,
                        error=str(error)[:2000],
                    )
                except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError):
                    pass
            elif root is not None:
                shutil.rmtree(root, ignore_errors=True)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error), "project_id": project_id})
            return

        worker = threading.Thread(
            target=run_project_worker,
            args=(self.server, store, project_id),
            daemon=True,
            name=f"lapian-{project_id}",
        )
        try:
            worker.start()
        except RuntimeError as error:
            self.server.mark_project_inactive(project_id)
            store.update(
                project_id,
                status="failed",
                stage="无法启动分析线程",
                progress=8,
                progress_phase="failed",
                progress_detail="片源副本已保留",
                error=str(error)[:2000],
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error), "project_id": project_id})
            return
        self._send_json(HTTPStatus.ACCEPTED, {"project": manifest})

    def do_PUT(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/settings/projects-root":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self._read_json()
            value = payload.get("path")
            if not isinstance(value, str):
                raise ValueError("保存位置必须是路径字符串。")
            settings = self.server.switch_projects_root(value)
        except PermissionError as error:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return
        except RuntimeError as error:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except OSError as error:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)[:1000]})
            return
        self._send_json(HTTPStatus.OK, {"settings": settings})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        project_id = match.group(1)
        try:
            manifest = self.server.delete_project(project_id)
        except RuntimeError as error:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        except (ValueError, FileNotFoundError):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "project not found"})
            return
        except OSError as error:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)[:1000]})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "deleted": {
                    "project_id": manifest.get("project_id", project_id),
                    "title": manifest.get("title", ""),
                }
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    settings_path = os.getenv("LAPIAN_SETTINGS_PATH", "").strip()
    parser.add_argument("--host", default=os.getenv("LAPIAN_WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LAPIAN_WORKBENCH_PORT", "8765")))
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=None,
    )
    parser.add_argument("--settings-path", type=Path, default=Path(settings_path) if settings_path else None)
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
    settings_store: LocalSettingsStore,
    settings_locked: bool,
    max_upload_bytes: int,
    open_browser: bool,
) -> None:
    server = WorkbenchHTTPServer((host, port), WorkbenchHandler)
    server.store = ProjectStore(projects_root)
    server.settings_store = settings_store
    server.settings_locked = settings_locked
    recovered = server.store.recover_interrupted()
    server.max_upload_bytes = max_upload_bytes
    url = f"http://{host}:{server.server_address[1]}"
    print(f"Lapian local workbench: {url}")
    print(f"Projects root: {server.store.root}")
    if recovered:
        print(f"Recovered interrupted projects: {recovered}")
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
    settings_store = LocalSettingsStore(args.settings_path)
    projects_root = resolve_projects_root(
        cli_root=args.projects_root,
        settings=settings_store,
        env_root=os.getenv("LAPIAN_PROJECTS_ROOT"),
    )
    max_upload_bytes = max(1, int(args.max_upload_gb * 1024 * 1024 * 1024))
    serve(
        host=args.host,
        port=args.port,
        projects_root=projects_root,
        settings_store=settings_store,
        settings_locked=args.projects_root is not None,
        max_upload_bytes=max_upload_bytes,
        open_browser=not args.no_open,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
