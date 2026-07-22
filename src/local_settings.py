"""Persistent local settings shared by the workbench and optional AI gateway."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS_ROOT = ROOT / "outputs" / "projects"


def default_settings_path() -> Path:
    configured = os.getenv("LAPIAN_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".config" / "lapian-chaoren" / "settings.json").resolve()


def normalize_projects_root(
    value: str | os.PathLike[str],
    *,
    base: Path | None = None,
    create: bool = True,
    require_absolute: bool = False,
) -> Path:
    raw = os.fspath(value).strip()
    if not raw:
        raise ValueError("保存位置不能为空。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if require_absolute:
            raise ValueError("请输入绝对路径，或使用“选择文件夹”。")
        path = (base or Path.cwd()) / path
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError("不能把磁盘根目录直接设为项目保存位置。")
    if path.exists() and not path.is_dir():
        raise ValueError("保存位置指向的是文件，请选择文件夹。")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError("当前用户没有读写这个文件夹的权限。")
    return path


class LocalSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_settings_path()).expanduser().resolve()
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.is_file():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

    def projects_root(self) -> Path | None:
        value = self.read().get("projects_root")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return normalize_projects_root(value, create=False, require_absolute=True)
        except (ValueError, PermissionError):
            return None

    def save_projects_root(self, root: Path) -> Path:
        normalized = normalize_projects_root(root, create=True, require_absolute=True)
        with self._lock:
            payload = self.read()
            payload["projects_root"] = str(normalized)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        return normalized


def resolve_projects_root(
    *,
    cli_root: Path | None = None,
    settings: LocalSettingsStore | None = None,
    env_root: str | None = None,
) -> Path:
    if cli_root is not None:
        return normalize_projects_root(cli_root, base=ROOT)
    stored = settings.projects_root() if settings else None
    if stored is not None:
        return normalize_projects_root(stored)
    if env_root and env_root.strip():
        return normalize_projects_root(env_root, base=ROOT)
    return normalize_projects_root(DEFAULT_PROJECTS_ROOT)
