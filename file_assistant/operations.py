"""Metadata-only scans and review planning. No mutations or content reads.

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

from .models import (
    Entry, OperationPlan, OperationRequest, OrganizationPlan, PathObservation, ScanResult,
)
from .ollama_client import parse_proposal
from .workspace import (
    _inspect_component, validate_destination, validate_path, validate_workspace,
)


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


def _review_child(
    root: Path, expected: PathObservation, root_details: os.stat_result,
) -> tuple[Path, os.stat_result]:
    """Revalidate one observed root child without reading its contents."""
    path = validate_path(root, expected.relative_path)
    if path.parent != root or path.name != expected.relative_path:
        raise ValueError(f"Scanned path changed: {expected.relative_path}")
    details = _inspect_component(path)
    if _observe(expected.relative_path, details, root_details) != expected:
        raise ValueError(f"Scanned object changed: {expected.relative_path}")
    if details.st_dev != root_details.st_dev:
        raise ValueError("Cross-volume organization is unsupported.")
    return path, details


def build_organization_plan(
    workspace: Path, snapshot: ScanSnapshot, proposal: str | dict, *,
    proposal_scan_id: str, selected_file_ids: tuple[str, ...] | None = None,
) -> OrganizationPlan:
    """Build fresh immutable review data, never authorization or filesystem work.

    proposal_scan_id must be retained by trusted caller code when dispatching
    the proposal request. Pass the current scan and selected root here; never
    infer the response's scan ID from that current scan or from model output.
    This rejects delayed responses when file IDs recur in a newer scan.

    JSON text is preferred so duplicate keys remain detectable. Dictionaries
    are serialized and passed through the same T13 parser. All proposed moves
    must validate before selection; malformed/stale operations are not hidden
    by deselection. None selects all moves; an empty collection selects none.

    Checks use metadata only and are not atomic. T9 must revalidate at execution,
    including observing the identity of any newly created parent; absence of
    that parent is recorded here without inventing an identity.
    """
    if (not isinstance(snapshot, ScanSnapshot) or snapshot.root is None
            or snapshot.result.errors or snapshot.result.cancelled):
        raise ValueError("Planning requires a complete successful scan snapshot.")
    if (not isinstance(proposal_scan_id, str) or not proposal_scan_id
            or proposal_scan_id != snapshot.scan_id):
        raise ValueError("Proposal request does not match the current scan.")
    if isinstance(proposal, dict):
        try:
            proposal = json.dumps(proposal, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Proposal must contain JSON data only.") from exc
    parsed = parse_proposal(proposal, snapshot.manifest())
    proposed_ids = {move["file_id"] for move in parsed["moves"]}
    if selected_file_ids is None:
        selected = proposed_ids
    else:
        if (not isinstance(selected_file_ids, (tuple, list, set, frozenset))
                or any(not isinstance(item, str) for item in selected_file_ids)):
            raise ValueError("Selection must contain distinct proposed file IDs.")
        selected = set(selected_file_ids)
        if len(selected) != len(selected_file_ids) or not selected <= proposed_ids:
            raise ValueError("Selection must contain distinct proposed file IDs.")

    # A successful scan must retain its complete, unambiguous local evidence.
    observed = {item.relative_path: item for item in snapshot.observations}
    kinds = {item.filename: "file" for item in snapshot.files}
    kinds.update({name: "directory" for name in snapshot.folders})
    expected_kinds = {".": "directory", **kinds}
    if (len(observed) != len(snapshot.observations) or set(observed) != set(expected_kinds)
            or any(item.present is not True or item.kind != expected_kinds[name]
                   for name, item in observed.items())
            or len(snapshot.result.entries) != len(kinds)
            or {item.relative_path: item.kind for item in snapshot.result.entries} != kinds):
        raise ValueError("Scan observations are missing, duplicated or inconsistent.")

    root = validate_workspace(workspace)
    if root != snapshot.root:
        raise ValueError("Selected workspace does not match the scan root.")
    root_details = _inspect_component(root)
    root_observation = observed["."]
    if _observe(".", root_details) != root_observation:
        raise ValueError("Scanned root identity or metadata changed.")
    root_identity = (root_details.st_dev, root_details.st_ino)
    by_id = {item.file_id: item for item in snapshot.files}
    existing_folders = {name.casefold(): name for name in snapshot.folders}
    folder_states = {}
    new_folders = {}
    source_names, source_identities, destinations = set(), set(), set()
    review_moves = []

    for move in parsed["moves"]:
        item = by_id[move["file_id"]]
        source_observation = observed[item.filename]
        source, details = _review_child(root, source_observation, root_details)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError("Source must be a singly linked regular file.")
        identity = (details.st_dev, details.st_ino)
        if source.name.casefold() in source_names or identity in source_identities:
            raise ValueError("Duplicate organization source.")
        source_names.add(source.name.casefold())
        source_identities.add(identity)

        key = move["destination_folder"].casefold()
        if key not in folder_states:
            if key in existing_folders:
                folder = existing_folders[key]
                parent_observation = observed[folder]
                _, parent_details = _review_child(root, parent_observation, root_details)
                if not stat.S_ISDIR(parent_details.st_mode):
                    raise ValueError("Destination parent must be a directory.")
                is_new = False
            else:
                folder_path = validate_destination(root, move["destination_folder"])
                folder = folder_path.relative_to(root).as_posix()
                parent_observation = PathObservation(
                    folder, False, parent_device=root_identity[0], parent_file_id=root_identity[1],
                )
                parent_details = None
                is_new = True
                new_folders[folder] = parent_observation
            folder_states[key] = (folder, parent_observation, parent_details, is_new)
        folder, parent_observation, parent_details, is_new = folder_states[key]
        destination = validate_destination(
            root, f"{folder}/{source.name}", planned_folder=folder if is_new else None,
        )
        relative = destination.relative_to(root).as_posix()
        if (len(destination.relative_to(root).parts) != 2
                or destination.name != source.name):
            raise ValueError("Destination must preserve the basename in one root child.")
        if relative.casefold() in destinations:
            raise ValueError("Case-insensitive destination collision.")
        destinations.add(relative.casefold())
        absent_destination = PathObservation(
            relative, False,
            parent_device=parent_details.st_dev if parent_details else None,
            parent_file_id=parent_details.st_ino if parent_details else None,
        )
        request = OperationRequest("move", item.filename, relative)
        review_moves.append((item.file_id, request, (
            root_observation, source_observation, parent_observation, absent_destination,
        )))

    if _observe(".", _inspect_component(root)) != root_observation:
        raise ValueError("Scanned root changed during planning.")
    selected_moves = [item for item in review_moves if item[0] in selected]
    needed_folders = {item[1].destination.split("/", 1)[0] for item in selected_moves}
    operations = []
    for folder in sorted(needed_folders & new_folders.keys(), key=lambda name: (name.casefold(), name)):
        operations.append(OperationPlan(
            uuid4().hex, root, root_identity, OperationRequest("mkdir", destination=folder),
            (root_observation, new_folders[folder]),
        ))
    for _, request, observations in sorted(
        selected_moves, key=lambda item: (item[1].source.casefold(), item[1].source),
    ):
        operations.append(OperationPlan(uuid4().hex, root, root_identity, request, observations))
    return OrganizationPlan(uuid4().hex, snapshot.scan_id, root, root_identity, tuple(operations))
