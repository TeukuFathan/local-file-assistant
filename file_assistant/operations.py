"""Bounded, metadata-only directory scans. No filesystem mutations or content reads.

All scans are nonrecursive. A rejected/inaccessible entry makes the result
incomplete; known top-level Trash is excluded by policy. Validation reduces
mistakes but cannot prevent concurrent path substitution by other processes.
"""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .models import Entry, PathObservation, ScanResult
from .workspace import _inspect_component, validate_path, validate_workspace


# Counting every enumerated entry bounds files, folders, exclusions and errors.
MAX_SCAN_ENTRIES = 1_000
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class ManifestFile:
    file_id: str
    filename: str
    extension: str


@dataclass(frozen=True)
class ScanSnapshot:
    """Local metadata and opaque IDs, kept separate from the inference manifest.

    File IDs are scoped to scan_id and assigned in deterministic filename order.
    Observations include the scanned root and safe immediate children. They are
    observations at scan time, not authorization or an atomic filesystem snapshot.
    """

    scan_id: str
    root: Path | None
    result: ScanResult
    files: tuple[ManifestFile, ...]
    folders: tuple[str, ...]
    observations: tuple[PathObservation, ...]

    def manifest(self) -> dict:
        """Return only inference fields; partial scans must not supply proposals."""
        if self.result.errors or self.result.cancelled:
            raise ValueError("Cannot use an incomplete scan manifest.")
        return {
            "files": [
                {"file_id": item.file_id, "filename": item.filename,
                 "extension": item.extension}
                for item in self.files
            ],
            "destination_folders": list(self.folders),
        }


def _positive_limit(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def _observe(
    relative_path: str, details: os.stat_result,
    parent: os.stat_result | None = None,
) -> PathObservation:
    return PathObservation(
        relative_path=relative_path, present=True,
        device=details.st_dev, file_id=details.st_ino, size=details.st_size,
        modified_ns=details.st_mtime_ns,
        kind="directory" if stat.S_ISDIR(details.st_mode) else "file",
        link_count=details.st_nlink,
        parent_device=parent.st_dev if parent else None,
        parent_file_id=parent.st_ino if parent else None,
    )


def _read_directory(
    workspace: Path, user_path: str, max_entries: int,
) -> tuple[Path | None, ScanResult, tuple[PathObservation, ...]]:
    _positive_limit(max_entries, "max_entries")
    root = None
    entries = []
    observations = []
    errors = []
    try:
        root = validate_workspace(workspace)
        directory = validate_path(root, user_path)
        parent_details = _inspect_component(directory)
        if not stat.S_ISDIR(parent_details.st_mode):
            raise NotADirectoryError(f"Not a directory: {user_path}")
        observations.append(_observe(directory.relative_to(root).as_posix(), parent_details))

        with os.scandir(directory) as children:
            for count, child in enumerate(children, start=1):
                if count > max_entries:
                    errors.append(f"Directory entry limit exceeded ({max_entries}).")
                    break
                candidate = directory / child.name
                relative = candidate.relative_to(root).as_posix()
                if directory == root and child.name.casefold() == "trash":
                    continue
                try:
                    # Inspect without following links before canonical validation.
                    details = _inspect_component(candidate)
                    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                        raise ValueError("Unsupported filesystem object.")
                    canonical = validate_path(root, relative)
                    if canonical.parent != directory:
                        raise ValueError("Entry is no longer an immediate child.")
                    details = _inspect_component(canonical)
                    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                        raise ValueError("Unsupported filesystem object.")
                    relative = canonical.relative_to(root).as_posix()
                    observed = _observe(relative, details, parent_details)
                    entries.append(Entry(relative, observed.kind))
                    observations.append(observed)
                except (OSError, ValueError) as exc:
                    errors.append(f"{relative}: {exc}")
    except (OSError, ValueError) as exc:
        errors.append(f"{user_path}: {exc}")

    entries.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
    observations.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
    return root, ScanResult(tuple(entries), tuple(sorted(errors)), False), tuple(observations)


def list_entries(
    workspace: Path, user_path: str = ".", *, max_entries: int = MAX_SCAN_ENTRIES,
) -> ScanResult:
    """List safe immediate children in stable casefolded relative-path order.

    Errors describe incomplete results, including an inaccessible directory or
    entry, disappearing files, invalid paths, unsupported objects and redirects.
    An invalid root never falls back to a different directory.
    """
    return _read_directory(workspace, user_path, max_entries)[1]


def search_names(
    workspace: Path, query: str, user_path: str = ".", *,
    max_entries: int = MAX_SCAN_ENTRIES,
) -> ScanResult:
    """Match casefolded filename fragments among immediate files/directories.

    An empty query matches every eligible child. Search retains all scan errors,
    even when the inaccessible entry might not have matched the query.
    """
    if not isinstance(query, str):
        raise ValueError("Search query must be a string.")
    result = list_entries(workspace, user_path, max_entries=max_entries)
    entries = tuple(item for item in result.entries
                    if query.casefold() in Path(item.relative_path).name.casefold())
    return ScanResult(entries, result.errors, result.cancelled)


def scan_files(
    workspace: Path, *, max_entries: int = MAX_SCAN_ENTRIES,
    max_manifest_bytes: int = MAX_MANIFEST_BYTES,
) -> ScanSnapshot:
    """Capture eligible root files and safe child folder names for later review.

    Defaults allow at most 1,000 enumerated entries and a 256 KiB manifest,
    measured as UTF-8 json.dumps(..., ensure_ascii=True). Partial/oversized scans
    retain diagnostics but cannot produce a manifest. Multiply-linked files are
    excluded from move proposals. No source contents are opened or hashed.
    """
    _positive_limit(max_manifest_bytes, "max_manifest_bytes")
    root, result, observations = _read_directory(workspace, ".", max_entries)
    by_path = {item.relative_path: item for item in observations}
    files = []
    folders = []
    entries = []
    errors = list(result.errors)
    for entry in result.entries:
        if entry.kind == "directory":
            folders.append(entry.relative_path)
        else:
            if by_path[entry.relative_path].link_count != 1:
                errors.append(f"{entry.relative_path}: Multiple hard links are unsupported.")
                continue
            files.append(ManifestFile(f"f{len(files) + 1}", entry.relative_path,
                                      Path(entry.relative_path).suffix))
        entries.append(entry)
    result = ScanResult(tuple(entries), tuple(sorted(errors)), result.cancelled)
    snapshot = ScanSnapshot(uuid4().hex, root, result, tuple(files), tuple(folders), observations)
    if not result.errors:
        size = len(json.dumps(snapshot.manifest(), ensure_ascii=True).encode("utf-8"))
        if size > max_manifest_bytes:
            result = ScanResult(result.entries, (f"Manifest byte limit exceeded ({max_manifest_bytes}).",),
                                result.cancelled)
            snapshot = ScanSnapshot(snapshot.scan_id, root, result, snapshot.files,
                                    snapshot.folders, observations)
    return snapshot
