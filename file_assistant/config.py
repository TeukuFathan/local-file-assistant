"""Persistence for the explicitly selected workspace and simple preferences."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    workspace: Path | None = None


def default_config_path() -> Path:
    """Return the app-local configuration path, without creating directories."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise OSError("LOCALAPPDATA is not available; provide a config path explicitly.")
    return Path(local_app_data) / "FileAssistant" / "config.json"


def _validate_selected_root(root: Path) -> Path:
    """Perform basic selection checks; full authorization depends on T2."""
    if not root.is_absolute():
        raise ValueError("Workspace root must be absolute.")
    if root.is_symlink() or root.is_junction():
        raise ValueError("Workspace root cannot be a symbolic link or junction.")
    if not root.exists():
        raise FileNotFoundError("Workspace root does not exist.")
    if not root.is_dir():
        raise NotADirectoryError("Workspace root must be a directory.")
    return root


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config; missing, corrupt, or invalid state grants no workspace."""
    path = config_path if config_path is not None else default_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return AppConfig()
        raw_root = data.get("workspace")
        if raw_root is None:
            return AppConfig()
        if not isinstance(raw_root, str) or not raw_root:
            return AppConfig()
        try:
            root = _validate_selected_root(Path(raw_root))
        except (OSError, ValueError):
            return AppConfig()
        return AppConfig(workspace=root)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return AppConfig()


def save_config(config: AppConfig, config_path: Path | None = None) -> None:
    """Atomically persist config without creating or changing a workspace."""
    path = config_path if config_path is not None else default_config_path()
    root = (
        _validate_selected_root(config.workspace)
        if config.workspace is not None
        else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"workspace": str(root) if root is not None else None},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
