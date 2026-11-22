"""Metadata-only scans, review planning and explicitly approved organization.

All scans are nonrecursive. A rejected/inaccessible entry makes the result
incomplete; known top-level Trash is excluded by policy. Validation reduces
mistakes but cannot prevent concurrent path substitution by other processes.
"""

import json
import copy
import ctypes
import os
import stat
import weakref
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from .models import (
    Entry, OperationPlan, OperationRequest, OperationResult, OrganizationPlan,
    PathObservation, ScanResult,
)
from .ollama_client import parse_proposal
from .recovery import (
    ExecutionLock, RecoveryRequired, begin_batch, finish_unexecuted, is_complete,
    load_journals, mark_attempted, record_intent, record_outcome,
)
from .workspace import (
    _inspect_component, validate_destination, validate_path, validate_workspace,
)


# Counting every enumerated entry bounds files, folders, exclusions and errors.
MAX_SCAN_ENTRIES = 1_000
MAX_MANIFEST_BYTES = 256 * 1024

# Review/approval capabilities are local to this process, never deserialized
# from AI output or journals. Weak references avoid retaining discarded reviews.
_APPROVAL_LOCK = Lock()
_BUILT_PLANS = {}
_ISSUED_PLANS = weakref.WeakValueDictionary()
_APPROVALS = weakref.WeakKeyDictionary()


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
    plan = OrganizationPlan(uuid4().hex, snapshot.scan_id, root, root_identity, tuple(operations))
    key = plan.plan_id
    with _APPROVAL_LOCK:
        _BUILT_PLANS[key] = (weakref.ref(plan, lambda ref: _BUILT_PLANS.pop(key, None)), copy.deepcopy(plan))
    return plan


class PlanApproval:
    """Opaque one-use capability; only approve_organization_plan can issue it.

    This is a trusted Python/UI boundary, not isolation from code running in the
    same interpreter. Constructing this class directly confers no authority.
    """

    __slots__ = ("__weakref__",)


@dataclass(frozen=True)
class ExecutionReport:
    """Durable successes, the stopped operation, and mutations never submitted.

    not_started includes a stopped operation when intent/validation/cancellation
    prevented its native call. created_folders includes acknowledged mkdirs
    even when their outcome could not be persisted. No rollback is attempted.
    """

    plan_id: str | None
    status: str
    completed: tuple[OperationResult, ...]
    stopped: OperationResult | None
    not_started: tuple[str, ...]
    created_folders: tuple[str, ...]
    recovery_required: bool
    message: str


def approve_organization_plan(
    plan: OrganizationPlan, *, workspace: Path, scan_id: str, confirmed: bool,
) -> PlanApproval:
    """Call ONLY for an explicit trusted user confirmation of the current review.

    Root, scan, subset or proposal changes require a newly built plan and fresh
    confirmation. Neither approval nor execution accepts AI approval fields.
    One confirmation is issued per review, and each capability is consumed once.
    """
    with _APPROVAL_LOCK:
        owned = _BUILT_PLANS.get(plan.plan_id) if type(plan) is OrganizationPlan else None
        if (confirmed is not True or owned is None or owned[0]() is not plan
                or plan != owned[1] or workspace != plan.root or scan_id != plan.scan_id
                or plan.plan_id in _ISSUED_PLANS):
            raise ValueError("Explicit confirmation of the exact current app-owned plan is required.")
        approval = PlanApproval()
        _APPROVALS[approval] = (plan, copy.deepcopy(plan))
        _ISSUED_PLANS[plan.plan_id] = plan
        return approval


def _windows_api():
    if os.name != "nt":
        raise ValueError("Approved execution requires Windows.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    api.MoveFileExW.restype = wintypes.BOOL
    api.GetVolumePathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    api.GetVolumePathNameW.restype = wintypes.BOOL
    api.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    api.GetDriveTypeW.restype = wintypes.UINT
    api.GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    )
    api.GetVolumeInformationW.restype = wintypes.BOOL
    return api


def _volume_info(root):
    api = _windows_api()
    volume, filesystem = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(32)
    flags = wintypes.DWORD()
    if not api.GetVolumePathNameW(str(root), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    if Path(volume.value) != Path(root.anchor):
        raise ValueError("Unsupported volume mount boundary.")
    drive_type = api.GetDriveTypeW(volume.value)
    if not api.GetVolumeInformationW(volume.value, None, 0, None, None,
                                     ctypes.byref(flags), filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    return drive_type, filesystem.value, flags.value


def _preflight_storage(root):
    """Query actual Windows fixed NTFS storage, after shared redirect validation.

    GetDriveTypeW/GetVolumeInformationW establish media/filesystem type. They
    cannot certify the absence of synchronization software or hostile filters.
    """
    root = validate_workspace(root)
    media, filesystem, flags = _volume_info(root)
    if media != 3 or filesystem != "NTFS" or flags & 0x80000:
        raise ValueError("Execution supports only writable local fixed NTFS storage.")


def _move_no_replace(source, destination):
    """MoveFileExW flags=0: no replacement, cross-volume copy or delayed action.

    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
    Python validators narrow scope but do not close concurrent substitution races.
    """
    if not _windows_api().MoveFileExW("\\\\?\\" + str(source), "\\\\?\\" + str(destination), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def _create_directory(destination):
    destination.mkdir()  # No parents/exist_ok: an appeared folder is a conflict.


def _entry_signature(details):
    return (details.st_dev, details.st_ino, details.st_mode, details.st_size,
            details.st_mtime_ns, details.st_nlink, getattr(details, "st_file_attributes", 0))


def _directory_identity(details):
    return details.st_dev, details.st_ino, details.st_mode, getattr(details, "st_file_attributes", 0)


def _directory_view(path):
    """Capture immediate entry metadata only; never follow child redirects."""
    with os.scandir(path) as entries:
        # Windows DirEntry.stat may omit identity/link-count fields. lstat
        # obtains them without following the child or opening its contents.
        return {entry.name: _entry_signature((path / entry.name).lstat()) for entry in entries}


def _execution_observation(root, relative):
    path = validate_path(root, relative)
    if path.relative_to(root).as_posix() != relative:
        raise ValueError("Path spelling or canonical containment changed.")
    details = _inspect_component(path)
    if not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)):
        raise ValueError("Unsupported filesystem object.")
    parent = _inspect_component(path.parent) if relative != "." else None
    return _observe(relative, details, parent), details


class _ExecutionState:
    """Expected metadata plus exact directory-entry deltas for this batch only."""

    def __init__(self, plan):
        self.root = plan.root
        self.expected = {}
        for operation in plan.operations:
            request = operation.request
            if (request.action not in ("mkdir", "move") or operation.root != plan.root
                    or operation.root_identity != plan.root_identity):
                raise ValueError("Unsupported operation or inconsistent batch context.")
            for item in operation.observations:
                previous = self.expected.setdefault(item.relative_path, item)
                if previous != item:
                    raise ValueError("Inconsistent batch observations.")
        creations = {op.request.destination for op in plan.operations if op.request.action == "mkdir"}
        needed = {op.request.destination.split('/')[0] for op in plan.operations if op.request.action == "move"}
        if not creations <= needed or "." not in self.expected:
            raise ValueError("Unnecessary folder creation or missing root observation.")
        self.views, self.fixed_directories = {}, {}
        self.check()
        for relative, item in self.expected.items():
            if item.present and item.kind == "directory":
                self.fixed_directories[relative] = _directory_identity(_inspect_component(self.root / relative))
                self.views[relative] = _directory_view(self.root / relative)
        self.check()  # Detect changes during capture, before begin_batch or mkdir.

    def check(self):
        root = validate_workspace(self.root)
        if str(root) != str(self.root):
            raise ValueError("Selected root changed.")
        for relative, expected in self.expected.items():
            if expected.present:
                observed, _ = _execution_observation(root, relative)
                if observed != expected or observed.device != self.expected["."].device:
                    raise ValueError(f"Identity or metadata changed: {relative}")
                if observed.kind == "file" and observed.link_count != 1:
                    raise ValueError("Multiply hard-linked sources are unsupported.")
            else:
                parent = relative.split('/')[0]
                planned = parent if '/' in relative and not self.expected[parent].present else None
                path = validate_destination(root, relative, planned_folder=planned)
                if path.relative_to(root).as_posix() != relative:
                    raise ValueError("Destination spelling changed.")
        for relative, expected in self.views.items():
            if _directory_identity(_inspect_component(root / relative)) != self.fixed_directories[relative]:
                raise ValueError(f"Directory identity/attributes changed: {relative}")
            if _directory_view(root / relative) != expected:
                raise ValueError(f"Unexpected directory-entry changes: {relative}")

    def intent_observations(self, operation):
        return tuple(self.expected[item.relative_path] for item in operation.observations)

    def finish(self, operation):
        """Accept only proved entry deltas; never adopt replaced directories."""
        request = operation.request
        root_obs, root_details = _execution_observation(self.root, ".")
        previous_root = self.expected["."]
        if _directory_identity(root_details) != self.fixed_directories["."]:
            raise ValueError("Root identity or attributes changed during operation.")
        allowed_links = {previous_root.link_count}
        if request.action == "mkdir":
            allowed_links.add(previous_root.link_count + 1)
        if ((root_obs.device, root_obs.file_id, root_obs.kind)
                != (previous_root.device, previous_root.file_id, "directory")
                or root_obs.link_count not in allowed_links):
            raise ValueError("Root identity/type/link count changed during operation.")
        destination, details = _execution_observation(self.root, request.destination)
        root_view = dict(self.views["."])
        if request.action == "mkdir":
            if destination.kind != "directory" or destination.device != root_obs.device:
                raise ValueError("Created directory is unsafe or cross-volume.")
            parent_view = _directory_view(self.root / request.destination)
            if parent_view:
                raise ValueError("Unexpected entries in newly created directory.")
            root_view[request.destination] = _entry_signature(details)
            touched = {request.destination: destination}
            updated_views = {request.destination: parent_view}
        else:
            parent_name = request.destination.split('/')[0]
            parent, parent_details = _execution_observation(self.root, parent_name)
            previous_parent = self.expected[parent_name]
            if _directory_identity(parent_details) != self.fixed_directories[parent_name]:
                raise ValueError("Destination parent attributes changed during move.")
            if ((parent.device, parent.file_id, parent.kind, parent.link_count)
                    != (previous_parent.device, previous_parent.file_id, "directory", previous_parent.link_count)):
                raise ValueError("Destination parent identity changed during move.")
            source = self.expected[request.source]
            expected_destination = replace(source, relative_path=request.destination,
                                           parent_device=parent.device, parent_file_id=parent.file_id)
            if (destination != expected_destination
                    or _entry_signature(details) != self.views["."][request.source]):
                raise ValueError("Moved file identity/type/metadata does not match source.")
            validate_destination(self.root, request.source)
            parent_view = dict(self.views[parent_name])
            parent_view[request.source] = _entry_signature(details)
            if _directory_view(self.root / parent_name) != parent_view:
                raise ValueError("Unexpected destination directory-entry changes.")
            del root_view[request.source]
            root_view[parent_name] = _entry_signature(parent_details)
            touched = {parent_name: parent, request.source: PathObservation(
                request.source, False, parent_device=root_obs.device, parent_file_id=root_obs.file_id,
            )}
            updated_views = {parent_name: parent_view}
        if _directory_view(self.root) != root_view:
            raise ValueError("Unexpected root directory-entry changes during operation.")
        self.expected.update(touched)
        self.expected["."] = root_obs
        self.expected[request.destination] = destination
        # Only the proved mkdir supplies identity to its pending child targets.
        if request.action == "mkdir":
            self.fixed_directories[request.destination] = _directory_identity(details)
            for relative, item in tuple(self.expected.items()):
                if relative.startswith(request.destination + '/') and not item.present:
                    self.expected[relative] = replace(item, parent_device=destination.device,
                                                      parent_file_id=destination.file_id)
        self.views.update(updated_views)
        self.views["."] = root_view
        self.check()
        return destination


def execute_organization_plan(
    workspace: Path, plan: OrganizationPlan, *, scan_id: str,
    approval: PlanApproval | None = None, cancel_event: Event | None = None,
    state_directory: Path | None = None,
) -> ExecutionReport:
    """Execute only an explicitly approved, app-owned, unchanged review batch.

    The trusted UI must pass its CURRENT root/scan context. This synchronous
    function accepts a threading.Event for cancellation, never execution hooks.
    Empty/unapproved/initially cancelled requests make no managed writes.

    Directory mtime/size changes are accepted only on directories touched by our
    operation, after verifying their identities and exact expected entry deltas.
    Other metadata/entries must match. Path checks are not atomic with native
    calls; hostile substitution or change-and-restore within that gap remains
    possible. The app lock coordinates this app, not unrelated software.

    Cancellation closes only work this live executor knows it never submitted.
    Attempted/uncertain operations remain blocking until explicit recovery
    resolution. All records and created folders remain; never retry or resume.
    """
    completed, created = [], []
    plan_id = plan.plan_id if type(plan) is OrganizationPlan else None
    operation_ids = (tuple(op.plan_id for op in plan.operations if type(op) is OperationPlan)
                     if type(plan) is OrganizationPlan and type(plan.operations) is tuple else ())

    def report(status, message, *, stopped=None, remaining=operation_ids, recovery=False):
        return ExecutionReport(plan_id, status, tuple(completed), stopped, remaining,
                               tuple(created), recovery, message)

    with _APPROVAL_LOCK:
        approved = _APPROVALS.pop(approval, None) if type(approval) is PlanApproval else None
        if (approved is None or approved[0] is not plan or plan != approved[1]
                or workspace != plan.root or scan_id != plan.scan_id):
            return report("rejected", "Approval does not match the exact current plan/root/scan.")
    if cancel_event is not None and type(cancel_event) is not Event:
        return report("rejected", "Cancellation must use a trusted threading.Event.")
    if cancel_event is not None and cancel_event.is_set():
        return report("cancelled", "Cancelled before execution.")
    if not plan.operations:
        return report("success", "Empty plan; no operations.")
    try:
        with ExecutionLock(workspace, state_directory) as lock:
            journals = load_journals(lock)
            if (any(journal.plan.plan_id == plan.plan_id for journal in journals)
                    or any(not is_complete(journal) for journal in journals)):
                raise RecoveryRequired("Previous or unresolved batch blocks execution; no automatic resume.")
            try:
                _preflight_storage(plan.root)
                state = _ExecutionState(plan)
            except (OSError, ValueError) as exc:
                return report("rejected", f"Batch validation failed: {exc}")
            if cancel_event is not None and cancel_event.is_set():
                return report("cancelled", "Cancelled before journaling.")
            begin_batch(lock, plan)

            def cancelled(remaining):
                try:
                    finished = finish_unexecuted(lock, plan.plan_id, message="User cancelled before submitting remaining operations.")
                except (OSError, ValueError, RecoveryRequired) as exc:
                    return report("recovery_required", f"Cancellation finalization failed; preserve unresolved evidence: {exc}",
                                  remaining=remaining, recovery=True)
                return report("cancelled", "Cancelled; unsubmitted operations are terminal and records are retained.",
                              remaining=remaining, recovery=not is_complete(finished))

            for index, operation in enumerate(plan.operations):
                remaining = operation_ids[index:]
                if cancel_event is not None and cancel_event.is_set():
                    return cancelled(remaining)
                intent, submitted, published = False, False, False
                try:
                    state.check()
                    observations = state.intent_observations(operation)
                    record_intent(lock, plan.plan_id, operation.plan_id, observations=observations)
                    intent = True
                    state.check()  # Journal persistence may have allowed external changes.
                    if cancel_event is not None and cancel_event.is_set():
                        return cancelled(remaining)
                    source = plan.root / operation.request.source if operation.request.action == "move" else None
                    destination = plan.root / operation.request.destination
                    mark_attempted(lock, plan.plan_id, operation.plan_id)
                    submitted = True
                    if operation.request.action == "mkdir":
                        _create_directory(destination)
                        created.append(operation.request.destination)
                    else:
                        _move_no_replace(source, destination)
                    outcome = state.finish(operation)
                    published = True  # From here, never try to replace a failed outcome write.
                    record_outcome(lock, plan.plan_id, operation.plan_id, "success", observations=(outcome,))
                    completed.append(OperationResult(operation.plan_id, "success", "Operation completed and journaled."))
                except (OSError, ValueError, RecoveryRequired) as exc:
                    if intent and submitted and not published:
                        try:
                            record_outcome(lock, plan.plan_id, operation.plan_id,
                                           "recovery_required", message=str(exc)[:2000])
                        except (OSError, ValueError, RecoveryRequired):
                            pass  # Last durable intent remains the evidence; stop either way.
                    unresolved = True
                    try:
                        finished = finish_unexecuted(lock, plan.plan_id, message="Stopped before submitting remaining operations.")
                        unresolved = not is_complete(finished)
                    except (OSError, ValueError, RecoveryRequired):
                        pass  # Failed publication must retain blocking evidence.
                    status = "recovery_required" if unresolved else "failed"
                    stopped = OperationResult(operation.plan_id, status, str(exc))
                    return report(status, f"Stopped; preserve journals and review partial effects: {exc}",
                                  stopped=stopped, remaining=operation_ids[index + int(submitted):], recovery=unresolved)
            return report("success", "All approved operations completed and journaled.", remaining=())
    except (OSError, ValueError, RecoveryRequired) as exc:
        done = {item.plan_id for item in completed}
        return report("recovery_required", f"Execution blocked: {exc}",
                      remaining=tuple(item for item in operation_ids if item not in done), recovery=True)
