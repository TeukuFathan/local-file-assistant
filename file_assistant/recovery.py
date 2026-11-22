"""App-owned execution evidence and a Windows process lock; never an executor.

T9 must hold ONE ExecutionLock (using the same state directory in every app
instance) across recovery checks, intent persistence, execution and outcome
writes. A successful journal call is not approval or path revalidation. Never
retry an interrupted operation based on an inspection finding.

Each batch has one bounded version-1 JSON file, retained even after success.
Updates flush/fsync a temporary file in the same directory, close it and use
os.replace. This avoids partial JSON on ordinary process interruption. Windows
does not offer a portable directory-fsync guarantee here: rename metadata,
hardware caches and sudden power loss remain limitations. There is no claim of
a filesystem transaction or hostile-process isolation. Failed persistence stops
this lock session; an outcome failure leaves the previous durable intent.
"""

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path, PureWindowsPath

from .config import default_config_path
from .models import OperationPlan, OperationRequest, OrganizationPlan, PathObservation
from .workspace import _inspect_component, _validate_windows_component, validate_path, validate_workspace

if os.name == "nt":
    import msvcrt


MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_OPERATIONS = 2_000  # T5 allows 1,000 files; T6 can add at most one folder per file.
MAX_JOURNALS = 1_000  # Exceeding this requires explicit maintenance, never truncation.
_ID = re.compile(r"[0-9a-f]{32}\Z")
_STATUSES = {"pending", "intent", "success", "failed", "recovery_required", "not_executed"}
_TERMINAL = {"success", "not_executed"}


class RecoveryRequired(Exception):
    """Evidence is incomplete, unsupported or unsafe; do not execute."""


class PersistenceError(RecoveryRequired):
    """Intent must not start, or an already attempted operation is uncertain."""


class LockUnavailable(RecoveryRequired):
    """Another process owns the app's execution lock (or locking failed)."""


@dataclass(frozen=True)
class JournalRecord:
    operation_id: str
    status: str = "pending"
    intent_observations: tuple[PathObservation, ...] = ()
    outcome_observations: tuple[PathObservation, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class Journal:
    plan: OrganizationPlan
    records: tuple[JournalRecord, ...]
    resolution: "RecoveryResolution | None" = None


@dataclass(frozen=True)
class RecoveryFinding:
    batch_id: str
    operation_id: str
    conclusion: str
    detail: str
    observations: tuple[PathObservation, ...] = ()


@dataclass(frozen=True)
class RecoveryResolution:
    """Explicit acknowledgement; original operation outcomes remain unchanged."""

    message: str
    findings: tuple[RecoveryFinding, ...]


def is_complete(journal: Journal) -> bool:
    """Terminal evidence permits a NEW plan, never replay of the recorded one."""
    return journal.resolution is not None or all(record.status in _TERMINAL for record in journal.records)


def _require(condition, message="Malformed or unsupported journal."):
    if not condition:
        raise RecoveryRequired(message)


def _identifier(value):
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None,
             "Journal identities must be T6 UUID hex strings.")
    return value


def _root_path(value):
    _require(isinstance(value, str) and bool(value), "Invalid journal root.")
    path = Path(value)
    windows = PureWindowsPath(value)
    _require(path.is_absolute() and len(windows.drive) == 2
             and windows.drive[0].isalpha() and windows.drive[1] == ":"
             and ".." not in windows.parts and str(path) == value,
             "Journal root must be an absolute local path.")
    for part in windows.parts[1:]:
        _validate_windows_component(part)
    return path


def _relative(value, *, root=False):
    _require(isinstance(value, str) and bool(value), "Invalid relative journal path.")
    if root and value == ".":
        return ()
    parts = value.split("/")
    _require(len(parts) <= 2 and "\\" not in value
             and all(part not in ("", ".", "..") for part in parts)
             and parts[0].casefold() != "trash", "Unsafe journal path.")
    for part in parts:
        _validate_windows_component(part)
    return tuple(parts)


def _identity(value):
    _require(isinstance(value, (tuple, list)) and len(value) == 2
             and all(type(item) is int and item >= 0 for item in value),
             "Invalid filesystem identity.")
    return tuple(value)


def _separate(root, state):
    _require(not (state.is_relative_to(root) or root.is_relative_to(state)),
             "Workspace must not overlap application state.")


def _state_directory(path, *, create=False, workspace=None):
    """Validate existing ancestors before creating app-owned state directories."""
    _root_path(str(path))
    missing = []
    for component in (*reversed(path.parents), path):
        try:
            details = _inspect_component(component)
        except FileNotFoundError:
            _require(create, "Application state directory disappeared.")
            missing.append(component)
            continue
        _require(stat.S_ISDIR(details.st_mode), "Application state is not a directory.")
    if workspace is not None:
        _separate(workspace, path.resolve(strict=False))
    for component in missing:
        try:
            component.mkdir()  # Only app-owned state, after ancestor/overlap checks.
        except FileExistsError:
            pass  # A competing first launch may have created the same directory.
        _require(stat.S_ISDIR(_inspect_component(component).st_mode))
    return path.resolve(strict=True)


def _state_file(path):
    details = _inspect_component(path)
    _require(stat.S_ISREG(details.st_mode) and details.st_nlink == 1,
             "App-state files must be regular files with one hard link.")
    return details


class ExecutionLock:
    """Nonblocking Windows byte-range lock, released on close or process exit.

    Every instance MUST use the same trusted app-state directory, normally
    default_config_path().parent. An explicit directory is for configured app
    state/tests, never an AI-provided path. The empty lock file is retained:
    deleting or replacing it could split the coordination boundary.
    """

    def __init__(self, workspace: Path, state_directory: Path | None = None):
        self.workspace = _root_path(str(workspace))
        self.state_directory = Path(state_directory) if state_directory is not None else default_config_path().parent
        _root_path(str(self.state_directory))
        _separate(self.workspace, self.state_directory)
        self._fd = None
        self._poisoned = False
        self._batch_id = None
        self._attempted = set()

    def __enter__(self):
        _require(os.name == "nt", "Execution locking requires Windows.")
        _require(self._fd is None, "Execution lock is already held.")
        try:
            try:
                self.workspace = validate_workspace(self.workspace)
            except FileNotFoundError:
                # Shared validation checked every existing ancestor first. A
                # missing historical root must still permit recovery inspection.
                self.workspace = self.workspace.resolve(strict=False)
            self.state_directory = _state_directory(self.state_directory, create=True, workspace=self.workspace)
            _separate(self.workspace, self.state_directory)
            path = self.state_directory / "execution.lock"
            try:
                _state_file(path)
            except FileNotFoundError:
                pass
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)
            try:
                os.set_inheritable(descriptor, False)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                os.close(descriptor)
                raise LockUnavailable("App execution lock unavailable; do not execute.") from exc
            self._fd = descriptor
            self._poisoned = False
            self._batch_id = None
            self._attempted = set()
            self.check()
            return self
        except (OSError, ValueError) as exc:
            self.__exit__(None, None, None)
            raise RecoveryRequired(f"Cannot acquire safe app-state lock: {exc}") from exc
        except RecoveryRequired:
            self.__exit__(None, None, None)
            raise

    def check(self, *, writing=False):
        _require(self._fd is not None, "Hold the app execution lock for this operation.")
        if writing:
            _require(not self._poisoned, "Persistence failed; stop this execution session.")
        try:
            _state_directory(self.state_directory)
            actual = _state_file(self.state_directory / "execution.lock")
            held = os.fstat(self._fd)
            _require((actual.st_dev, actual.st_ino) == (held.st_dev, held.st_ino),
                     "Execution lock file identity changed.")
        except (OSError, ValueError) as exc:
            raise RecoveryRequired(f"App-state lock validation failed: {exc}") from exc

    def __exit__(self, *exc):
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)


def _keys(data, names):
    _require(type(data) is dict and set(data) == set(names), "Unknown/missing journal fields.")


def _observations(data):
    _require(type(data) is list and len(data) <= 4, "Invalid observation list.")
    result = []
    for raw in data:
        _keys(raw, (field.name for field in fields(PathObservation)))
        _relative(raw["relative_path"], root=True)
        _require(type(raw["present"]) is bool)
        for name in ("device", "file_id", "size", "link_count", "parent_device", "parent_file_id"):
            value = raw[name]
            _require(value is None or (type(value) is int and value >= 0))
        _require(raw["modified_ns"] is None or type(raw["modified_ns"]) is int)
        _require((raw["parent_device"] is None) == (raw["parent_file_id"] is None))
        metadata = ("device", "file_id", "size", "modified_ns", "kind", "link_count")
        if raw["present"]:
            _require(all(raw[name] is not None for name in metadata)
                     and raw["kind"] in ("file", "directory") and raw["link_count"] >= 1)
        else:
            _require(all(raw[name] is None for name in metadata))
        result.append(PathObservation(**raw))
    _require(len({item.relative_path.casefold() for item in result}) == len(result),
             "Duplicate observation paths.")
    return tuple(result)


def _evidence(operation, observations, *, intent=False):
    """Validate metadata relationships without accessing managed paths."""
    by_path = {item.relative_path: item for item in observations}
    request = operation.request
    destination = request.destination
    parent = destination.split("/")[0] if request.action == "move" else "."
    required = {".", destination} | ({request.source, parent} if request.action == "move" else set())
    _require(set(by_path) == required, "Incomplete operation observations.")
    root = by_path["."]
    _require(root.present and root.kind == "directory"
             and (root.device, root.file_id) == operation.root_identity
             and root.parent_device is None, "Inconsistent root observation.")
    _require(not by_path[destination].present, "Intent destination must be absent.")
    if request.action == "move":
        source, folder = by_path[request.source], by_path[parent]
        _require(source.present and source.kind == "file" and source.link_count == 1
                 and source.device == root.device
                 and (source.parent_device, source.parent_file_id) == operation.root_identity,
                 "Invalid move source evidence.")
        _require((folder.parent_device, folder.parent_file_id) == operation.root_identity,
                 "Invalid destination folder ancestry.")
        if folder.present:
            _require(folder.kind == "directory" and folder.device == root.device)
        if intent:
            _require(folder.present, "T9 must supply the created/existing parent identity before move intent.")
    folder = by_path[parent]
    _require((by_path[destination].parent_device, by_path[destination].parent_file_id)
             == ((folder.device, folder.file_id) if folder.present else (None, None)),
             "Invalid destination ancestry evidence.")
    return by_path


def _same_file(observation, expected):
    return (observation.present and observation.kind == expected.kind
            and (observation.device, observation.file_id, observation.size,
                 observation.modified_ns, observation.link_count)
            == (expected.device, expected.file_id, expected.size,
                expected.modified_ns, expected.link_count))


def _validate_journal(data, state):
    _require(type(data) is dict)
    _keys(data, ("version", "plan", "records") + (("resolution",) if "resolution" in data else ()))
    _require(type(data["version"]) is int and data["version"] == 1, "Unsupported journal version.")
    raw = data["plan"]
    _keys(raw, (field.name for field in fields(OrganizationPlan)))
    batch_id, scan_id = _identifier(raw["plan_id"]), _identifier(raw["scan_id"])
    root, identity = _root_path(raw["root"]), _identity(raw["root_identity"])
    _separate(root, state)
    _require(type(raw["operations"]) is list and len(raw["operations"]) <= MAX_OPERATIONS)
    operations, operation_ids, sources, destinations, folders = [], {batch_id}, set(), set(), {}
    source_identities = set()
    moved = False
    for item in raw["operations"]:
        _keys(item, (field.name for field in fields(OperationPlan)))
        operation_id = _identifier(item["plan_id"])
        _require(operation_id not in operation_ids)
        operation_ids.add(operation_id)
        _require(item["root"] == str(root) and _identity(item["root_identity"]) == identity)
        request_data = item["request"]
        _keys(request_data, (field.name for field in fields(OperationRequest)))
        request = OperationRequest(**request_data)
        _require(request.action in ("mkdir", "move"), "Only mkdir and move may be journaled.")
        parts = _relative(request.destination)
        destination_key = request.destination.casefold()
        _require(destination_key not in destinations, "Duplicate journal destinations.")
        destinations.add(destination_key)
        if request.action == "mkdir":
            _require(len(parts) == 1 and not moved, "Folder creations must precede moves.")
            folders[destination_key] = request.destination
        else:
            moved = True
            _require(len(_relative(request.source)) == 1 and len(parts) == 2
                     and parts[1] == request.source, "Move must preserve basename in one root child.")
            _require(request.source.casefold() not in sources, "Duplicate journal sources.")
            sources.add(request.source.casefold())
        operation = OperationPlan(operation_id, root, identity, request, _observations(item["observations"]))
        evidence = _evidence(operation, operation.observations)
        if request.action == "move":
            source = evidence[request.source]
            source_identity = (source.device, source.file_id)
            _require(source_identity not in source_identities, "Duplicate source identities.")
            source_identities.add(source_identity)
            if not evidence[parts[0]].present:
                _require(folders.get(parts[0].casefold()) == parts[0], "Missing planned mkdir.")
        operations.append(operation)
    _require(not sources & (set(folders) | {op.request.destination.split('/')[0].casefold()
                                         for op in operations}), "File/folder conflict.")
    raw_records = data["records"]
    _require(type(raw_records) is list and len(raw_records) == len(operations))
    records, stopped, created = [], False, {}
    for operation, raw_record in zip(operations, raw_records):
        _keys(raw_record, (field.name for field in fields(JournalRecord)))
        _require(raw_record["operation_id"] == operation.plan_id)
        status = raw_record["status"]
        _require(isinstance(status, str) and status in _STATUSES)
        _require(isinstance(raw_record["message"], str) and len(raw_record["message"]) <= 2_000)
        intent_obs = _observations(raw_record["intent_observations"])
        outcome_obs = _observations(raw_record["outcome_observations"])
        _require({item.relative_path for item in outcome_obs}
                 <= {item.relative_path for item in operation.observations},
                 "Outcome observations must refer to this operation.")
        if status == "pending":
            _require(not intent_obs and not outcome_obs and not raw_record["message"])
        if status == "not_executed":
            _require(not outcome_obs and bool(raw_record["message"].strip()),
                     "Nonexecution requires a reason and cannot contain an executed outcome.")
        if intent_obs or status not in ("pending", "not_executed"):
            _require(not stopped, "Operations after an unfinished operation cannot have executed.")
            evidence = _evidence(operation, intent_obs, intent=True)
            original = {item.relative_path: item for item in operation.observations}
            if operation.request.action == "move":
                source = operation.request.source
                _require(intent_obs and evidence[source] == original[source], "Source evidence changed.")
                parent = operation.request.destination.split('/')[0]
                if original[parent].present:
                    _require((evidence[parent].device, evidence[parent].file_id)
                             == (original[parent].device, original[parent].file_id))
                else:
                    _require(parent in created and (evidence[parent].device, evidence[parent].file_id)
                             == (created[parent].device, created[parent].file_id),
                             "Created parent differs from the recorded mkdir outcome.")
            if status == "intent":
                _require(not outcome_obs and not raw_record["message"])
            if status == "success":
                _require(len(outcome_obs) == 1 and outcome_obs[0].present
                         and outcome_obs[0].relative_path == operation.request.destination,
                         "Success requires observed destination identity.")
                outcome = outcome_obs[0]
                parent = evidence[operation.request.destination.split('/')[0]] if operation.request.action == "move" else evidence["."]
                _require((outcome.parent_device, outcome.parent_file_id) == (parent.device, parent.file_id))
                if operation.request.action == "move":
                    _require(_same_file(outcome, evidence[operation.request.source]), "Success identity mismatch.")
                else:
                    _require(outcome.kind == "directory" and outcome.device == identity[0])
                    created[operation.request.destination] = outcome
        if status != "success":
            stopped = True
        records.append(JournalRecord(operation.plan_id, status, intent_obs, outcome_obs, raw_record["message"]))
    journal = Journal(OrganizationPlan(batch_id, scan_id, root, identity, tuple(operations)), tuple(records))
    if "resolution" in data:
        resolution = data["resolution"]
        _keys(resolution, ("message", "findings"))
        _reason(resolution["message"])
        _require(type(resolution["findings"]) is list)
        unfinished = [(op, record) for op, record in zip(operations, records) if record.status not in _TERMINAL]
        _require(bool(unfinished) and len(unfinished) == len(resolution["findings"]),
                 "Resolution must preserve a review of every unfinished operation.")
        findings = []
        for (operation, record), raw_finding in zip(unfinished, resolution["findings"]):
            _keys(raw_finding, (field.name for field in fields(RecoveryFinding)))
            _require(raw_finding["batch_id"] == batch_id and raw_finding["operation_id"] == operation.plan_id)
            conclusion = raw_finding["conclusion"]
            _require(isinstance(conclusion, str) and conclusion in {
                "not_started", "source_only", "destination_only", "both_present", "neither_present",
                "identity_mismatch", "directory_absent", "uncertain",
            })
            _reason(raw_finding["detail"])
            observations = _observations(raw_finding["observations"])
            paths = {item.relative_path for item in observations}
            if record.status == "pending":
                _require(conclusion == "not_started" and not observations)
            else:
                _require(conclusion != "not_started", "A recorded intent cannot be inferred never submitted.")
                expected_paths = {item.relative_path for item in operation.observations}
                _require(paths == expected_paths or (len(observations) == 1
                         and observations[0].relative_path == "." and not observations[0].present),
                         "Unsafe/incomplete inspection cannot be resolved.")
            findings.append(RecoveryFinding(batch_id, operation.plan_id, conclusion,
                                             raw_finding["detail"], observations))
        journal = replace(journal, resolution=RecoveryResolution(resolution["message"], tuple(findings)))
    return journal


def _payload(journal):
    plan = asdict(journal.plan)
    plan["root"] = str(journal.plan.root)
    for operation in plan["operations"]:
        operation["root"] = str(operation["root"])
    data = {"version": 1, "plan": plan, "records": [asdict(item) for item in journal.records]}
    if journal.resolution is not None:
        data["resolution"] = asdict(journal.resolution)
    return json.dumps(data,
                      ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        _require(key not in data, "Duplicate JSON journal keys.")
        data[key] = value
    return data


def _decode(payload, state):
    try:
        _require(len(payload) <= MAX_JOURNAL_BYTES, "Journal byte limit exceeded.")
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda value: _require(False, "Nonfinite JSON number."))
        return _validate_journal(data, state)
    except (ValueError, TypeError, OSError, RecursionError, KeyError) as exc:
        raise RecoveryRequired(f"Invalid journal: {exc}") from exc


def load_journals(lock: ExecutionLock) -> tuple[Journal, ...]:
    """Load bounded strict records under the lock; missing state is not success."""
    lock.check()
    try:
        journals = []
        for path in lock.state_directory.iterdir():
            if not path.name.casefold().startswith("journal-"):
                continue
            _require(path.name.endswith(".json"), "Unrecognized journal file; preserve it for recovery.")
            batch_id = _identifier(path.name[8:-5])
            _require(path.name == f"journal-{batch_id}.json")
            details = _state_file(path)
            _require(details.st_size <= MAX_JOURNAL_BYTES, "Journal byte limit exceeded.")
            with path.open("rb") as stream:
                actual = os.fstat(stream.fileno())
                _require((actual.st_dev, actual.st_ino) == (details.st_dev, details.st_ino))
                journal = _decode(stream.read(MAX_JOURNAL_BYTES + 1), lock.state_directory)
            _require(journal.plan.plan_id == batch_id, "Journal filename/batch identity mismatch.")
            journals.append(journal)
            _require(len(journals) <= MAX_JOURNALS, "Journal count limit exceeded; do not truncate.")
        return tuple(sorted(journals, key=lambda item: item.plan.plan_id))
    except (OSError, ValueError) as exc:
        raise RecoveryRequired(f"Journal state inaccessible or unsafe: {exc}") from exc


def _persist(lock, journal, *, outcome=False):
    lock.check(writing=True)
    try:
        payload = _payload(journal)
        _decode(payload, lock.state_directory)  # Identical validation on writes/loads.
    except (TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise RecoveryRequired(f"Invalid journal data; do not execute: {exc}") from exc
    path = lock.state_directory / f"journal-{journal.plan.plan_id}.json"
    temporary = None
    try:
        try:
            _state_file(path)
        except FileNotFoundError:
            pass
        with tempfile.NamedTemporaryFile(mode="w+b", dir=lock.state_directory,
                                         prefix=".journal-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            if stream.write(payload) != len(payload):
                raise OSError("Incomplete temporary journal write.")
            stream.flush()
            os.fsync(stream.fileno())
        lock.check(writing=True)
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, RecoveryRequired) as exc:
        lock._poisoned = True
        message = ("Outcome persistence failed; operation uncertain, stop execution."
                   if outcome else "Intent/batch persistence failed; execution must not start.")
        raise PersistenceError(message) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass  # An app-owned unpublished temp is never an execution authorization.


def begin_batch(lock: ExecutionLock, plan: OrganizationPlan) -> Journal:
    """Persist a pending batch. Never overwrite previous or unresolved evidence."""
    lock.check(writing=True)
    journals = load_journals(lock)
    _require(isinstance(plan, OrganizationPlan) and plan.root == lock.workspace,
             "Batch root must match the trusted lock context.")
    _require(not any(item.plan.plan_id == plan.plan_id for item in journals), "Batch already recorded; no replay.")
    _require(all(is_complete(item) for item in journals),
             "Unresolved journal blocks new execution.")
    _require(len(journals) < MAX_JOURNALS, "Journal count limit reached.")
    journal = Journal(plan, tuple(JournalRecord(operation.plan_id) for operation in plan.operations))
    _persist(lock, journal)
    lock._batch_id = plan.plan_id
    lock._attempted = set()
    return journal


def _operation(lock, batch_id, operation_id):
    lock.check(writing=True)
    _identifier(batch_id)
    _identifier(operation_id)
    _require(lock._batch_id == batch_id, "Interrupted batches may be inspected, never automatically resumed.")
    journals = load_journals(lock)
    _require(all(is_complete(journal) for journal in journals if journal.plan.plan_id != batch_id),
             "Another unresolved batch blocks execution.")
    journal = next((item for item in journals if item.plan.plan_id == batch_id), None)
    _require(journal is not None, "Batch journal missing; do not execute.")
    _require(journal.resolution is None, "Resolved batches cannot resume.")
    index = next((i for i, item in enumerate(journal.records) if item.operation_id == operation_id), None)
    _require(index is not None, "Unknown journal operation.")
    _require(all(item.status == "success" for item in journal.records[:index]),
             "Previous operation unfinished; stop execution.")
    return journal, index


def record_intent(lock: ExecutionLock, batch_id: str, operation_id: str, *,
                  observations: tuple[PathObservation, ...] | None = None) -> Journal:
    """Persist intent BEFORE an effect. T9 supplies fresh metadata after mkdir.

    Original plan observations can be used only while sufficient. A move into
    a planned folder requires its observed identity, not T6's absent placeholder.
    This function validates evidence shape, not the current filesystem or approval.
    """
    journal, index = _operation(lock, batch_id, operation_id)
    _require(journal.records[index].status == "pending", "Operation already attempted; do not retry.")
    operation = journal.plan.operations[index]
    record = JournalRecord(operation_id, "intent", tuple(observations) if observations is not None else operation.observations)
    updated = replace(journal, records=journal.records[:index] + (record,) + journal.records[index + 1:])
    _persist(lock, updated)
    return updated


def record_outcome(lock: ExecutionLock, batch_id: str, operation_id: str, status: str, *,
                   observations: tuple[PathObservation, ...] = (), message: str = "") -> Journal:
    """Record a reported outcome; failure/uncertainty stops this batch.

    Success requires destination identity evidence, including a newly created
    directory's identity. Failed/uncertain outcomes remain unresolved; neither
    reporting nor inspection clears evidence or permits a retry.
    """
    journal, index = _operation(lock, batch_id, operation_id)
    _require(journal.records[index].status == "intent", "No durable intent for this outcome.")
    _require(isinstance(status, str) and status in ("success", "failed", "recovery_required"), "Unsupported outcome.")
    lock._attempted.add(operation_id)  # Even an unpublished outcome must remain uncertain.
    record = replace(journal.records[index], status=status, outcome_observations=tuple(observations), message=message)
    updated = replace(journal, records=journal.records[:index] + (record,) + journal.records[index + 1:])
    _persist(lock, updated, outcome=True)
    return updated


def _reason(message):
    _require(isinstance(message, str) and bool(message.strip()) and len(message) <= 2_000,
             "A nonempty reason of at most 2,000 characters is required.")


def mark_attempted(lock: ExecutionLock, batch_id: str, operation_id: str) -> None:
    """Call immediately BEFORE the native mutation, even if that call may fail.

    Process-local submission knowledge can prove nonexecution only while this
    executor still owns its lock session. After restart, intent stays uncertain.
    """
    journal, index = _operation(lock, batch_id, operation_id)
    _require(journal.records[index].status == "intent" and operation_id not in lock._attempted,
             "Native mutation requires an unsubmitted durable intent.")
    lock._attempted.add(operation_id)


def finish_unexecuted(lock: ExecutionLock, batch_id: str, *, message: str) -> Journal:
    """Close only pending/unsubmitted work known to this live executor.

    Keep success, attempted intent and failed/uncertain outcomes unchanged.
    This is not a recovery inference, retry or permission to reuse the plan.
    A persistence failure leaves the preceding journal blocking new execution.
    """
    lock.check(writing=True)
    _reason(message)
    _require(lock._batch_id == batch_id, "Only the original live executor knows nonexecution.")
    journal = next((item for item in load_journals(lock) if item.plan.plan_id == batch_id), None)
    _require(journal is not None and journal.resolution is None, "Active journal missing or resolved.")
    records = tuple(replace(record, status="not_executed", message=message)
                    if record.status == "pending" or (record.status == "intent"
                        and record.operation_id not in lock._attempted) else record
                    for record in journal.records)
    updated = replace(journal, records=records)
    if updated != journal:
        _persist(lock, updated)
    lock._batch_id = None  # This executor may never resume the stopped batch.
    return updated


def resolve_recovery(
    lock: ExecutionLock, journal: Journal, findings: tuple[RecoveryFinding, ...], *,
    confirmed: bool, message: str,
) -> Journal:
    """Record explicit trusted acknowledgement of an exact recovery review.

    Caller supplies the loaded journal and inspect_recovery findings for this
    batch after user review. Recheck both the record and live metadata under
    the app lock. Acknowledgement retains original uncertainty and all evidence;
    it permits a fresh reviewed plan, never retries/repairs/resumes this batch.
    Unsafe/inaccessible paths must be made inspectable before resolution.
    """
    lock.check(writing=True)
    _require(confirmed is True and isinstance(journal, Journal), "Explicit recovery confirmation required.")
    _reason(message)
    _require(lock.workspace == journal.plan.root, "Recovery root context does not match.")
    current = next((item for item in load_journals(lock) if item.plan.plan_id == journal.plan.plan_id), None)
    _require(current == journal and not is_complete(journal), "Recovery journal changed or is already terminal.")
    fresh = tuple(item for item in inspect_recovery(lock) if item.batch_id == journal.plan.plan_id)
    _require(type(findings) is tuple and bool(fresh) and findings == fresh,
             "Recovery findings changed or the review is incomplete.")
    updated = replace(journal, resolution=RecoveryResolution(message, fresh))
    _persist(lock, updated)
    if lock._batch_id == journal.plan.plan_id:
        lock._batch_id = None
    return updated


def _current(root, relative):
    """Validate ancestors first; a missing path is absence, never a redirect."""
    try:
        path = validate_path(root, relative)
    except FileNotFoundError:
        return None
    _require(path.relative_to(root).as_posix() == relative, "Path spelling or containment changed.")
    details = _inspect_component(path)
    _require(stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode), "Unsupported filesystem object.")
    return details


def _matches(details, expected):
    return (details is not None and (details.st_dev, details.st_ino, details.st_size,
                                    details.st_mtime_ns, details.st_nlink)
            == (expected.device, expected.file_id, expected.size, expected.modified_ns, expected.link_count)
            and stat.S_ISREG(details.st_mode))


def _inspect_operation(operation, record):
    try:
        root = validate_workspace(operation.root)
    except FileNotFoundError:
        # Existing ancestors were validated before the missing component.
        return "uncertain", "Recorded root is missing.", (PathObservation(".", False),)
    details = _inspect_component(root)
    _require(root == operation.root, "Root canonical path changed.")
    observed = {item.relative_path: item for item in record.intent_observations}
    destination = _current(root, operation.request.destination)
    current = {".": details, operation.request.destination: destination}
    if operation.request.action == "move":
        parent_name = operation.request.destination.split('/')[0]
        current[parent_name] = _current(root, parent_name)
        current[operation.request.source] = _current(root, operation.request.source)

    def finding(conclusion, detail):
        evidence = []
        for item in operation.observations:
            relative = item.relative_path
            actual = current[relative]
            parent = current.get(relative.rsplit('/', 1)[0] if '/' in relative else ".") if relative != "." else None
            ancestry = {"parent_device": parent.st_dev if parent else None,
                        "parent_file_id": parent.st_ino if parent else None}
            if actual is None:
                evidence.append(PathObservation(relative, False, **ancestry))
            else:
                evidence.append(PathObservation(
                    relative, True, actual.st_dev, actual.st_ino, actual.st_size,
                    actual.st_mtime_ns, "directory" if stat.S_ISDIR(actual.st_mode) else "file",
                    actual.st_nlink, **ancestry,
                ))
        return conclusion, detail, tuple(evidence)

    if (details.st_dev, details.st_ino) != operation.root_identity:
        return finding("uncertain", "Root identity changed.")
    if operation.request.action == "mkdir":
        if destination is None:
            return finding("directory_absent", "No directory is present; do not automatically retry creation.")
        return finding("uncertain", "An appeared path cannot be attributed to an interrupted mkdir intent.")
    parent = current[parent_name]
    expected_parent = observed[parent_name]
    if not (parent is not None and stat.S_ISDIR(parent.st_mode)
            and (parent.st_dev, parent.st_ino) == (expected_parent.device, expected_parent.file_id)):
        return finding("uncertain", "Destination parent missing or replaced.")
    source = current[operation.request.source]
    if source is not None and destination is not None:
        return finding("both_present", "Both paths exist; outcome uncertain, preserve both.")
    if source is None and destination is None:
        return finding("neither_present", "Neither path exists; outcome uncertain, preserve the journal.")
    expected = observed[operation.request.source]
    if not _matches(source if source is not None else destination, expected):
        return finding("identity_mismatch", "Recorded file identity/type/metadata does not match; outcome uncertain.")
    if source is not None:
        return finding("source_only", "Recorded file remains at source, destination absent; observation only, no retry.")
    return finding("destination_only", "Recorded file is at destination, source absent; observation only, no automatic resolution.")


def inspect_recovery(lock: ExecutionLock) -> tuple[RecoveryFinding, ...]:
    """Inspect unfinished evidence using metadata only, without changing state.

    Findings are observations, never authorization, automatic repair or proof
    against hostile concurrent changes. Corrupt/inaccessible journals raise
    RecoveryRequired; inaccessible/redirected managed paths yield uncertainty.
    """
    findings = []
    for journal in load_journals(lock):
        if journal.resolution is not None:
            continue
        for operation, record in zip(journal.plan.operations, journal.records):
            if record.status in _TERMINAL:
                continue
            observations = ()
            if record.status == "pending":
                conclusion, detail = "not_started", "No durable intent was recorded; do not execute during recovery."
            else:
                try:
                    conclusion, detail, observations = _inspect_operation(operation, record)
                except (OSError, ValueError, RecoveryRequired) as exc:
                    conclusion, detail = "uncertain", str(exc)
            findings.append(RecoveryFinding(journal.plan.plan_id, operation.plan_id, conclusion, detail, observations))
    return tuple(findings)
