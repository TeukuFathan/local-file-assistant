"""Persistence for the explicitly selected workspace and simple preferences."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .workspace import validate_workspace


@dataclass(frozen=True)
class AppConfig:
    workspace: Path | None = None


def default_config_path() -> Path:
    """Return the app-local configuration path, without creating directories."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise OSError("LOCALAPPDATA is not available; provide a config path explicitly.")
    return Path(local_app_data) / "FileAssistant" / "config.json"


def _validate_selected_root(root: Path, config_path: Path) -> Path:
    """Apply shared root policy and keep managed content separate from app state."""
    canonical_root = validate_workspace(root)
    # The state directory may not exist on first save. Resolve existing ancestors
    # and compare components, not string prefixes, before creating anything.
    state_directory = config_path.parent.resolve(strict=False)
    if (state_directory.is_relative_to(canonical_root)
            or canonical_root.is_relative_to(state_directory)):
        raise ValueError("Workspace must not overlap application state.")
    return canonical_root


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
            root = _validate_selected_root(Path(raw_root), path)
        except (OSError, ValueError):
            return AppConfig()
        return AppConfig(workspace=root)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return AppConfig()


def save_config(config: AppConfig, config_path: Path | None = None) -> None:
    """Atomically persist config without creating or changing a workspace."""
    path = config_path if config_path is not None else default_config_path()
    root = (
        _validate_selected_root(config.workspace, path)
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
