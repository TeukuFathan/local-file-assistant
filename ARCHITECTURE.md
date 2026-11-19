# Windows File Assistant: architecture proposal

Status: M0 scope and interface decisions recorded on 2026-11-19 under the user's request to complete T0/T1. Remaining implementation is a proposed roadmap, not blanket authorization. The [safety contract](./SAFETY_CONTRACT.md) remains the behavior baseline. The developer remains the implementer unless a task is explicitly delegated. Later architecture changes require approval under [AGENTS.md](./AGENTS.md).

## 1. Current state and evidence

Reviewed all existing Python files and both Markdown instructions, plus the [original guide](./Windows_Local_File_Assistant_Guide.docx).

- [workspace.py](./file_assistant/workspace.py) contains a useful directory predicate and an unfinished validator. Preserve these functions and build on their checks.
- The validator rejects drive/root paths, `..`, the literal top-level Trash name, and symlinks/junctions from the workspace downward. Junction checks now cover the root and every requested component. It does not validate workspace ancestors, confirm resolved containment, validate Windows names, or return a Path on success.
- The directory predicate follows links and is not a security boundary. Workspace configuration is currently a hardcoded sample path.
- [operations.py](./file_assistant/operations.py), [__main__.py](./file_assistant/__main__.py), [__init__.py](./file_assistant/__init__.py), and [test_operations.py](./tests/test_operations.py) are empty.
- [test_workspace.py](./tests/test_workspace.py) has 15 test methods, grouped and explained by function. Preserve them. Several link tests use mocks; there is a real junction test and a real symlink test that can skip without Windows privileges. The nested Trash test currently checks only absence of an exception, not a successful return value.
- No packaged desktop product, recovery implementation, or operation layer exists yet. At the 2026-11-19 implementation checkpoint, unittest discovery ran 15 tests: 14 passed and the real-symlink test skipped for Windows permissions. The expanded real-junction test passes requested-folder, ancestor-folder, and workspace-root cases. This does not certify the unfinished validator as safe for file operations.

## 2. Documentation conflicts and proposed resolutions

The safety contract is the agreed behavior baseline. The older guide is historical design material; conflicting examples must not be implemented silently.

| Conflict | Proposed resolution |
| --- | --- |
| Guide: single script, implicit workspace creation. Current code: package. Contract: explicitly configured root. | Keep the existing package; require an existing, explicitly selected root. No directory creation merely from listing. |
| Guide: Python 3.11+. Code uses `Path.is_junction()`. | Declare Python 3.12+ as the source minimum; pin and test a specific interpreter for each packaged release. Do not introduce a compatibility layer without a need. |
| Guide: open files in associated applications. Contract: never open/launch user files. | No open, shell execution, thumbnails, content preview, or association launch in v1. Metadata access is allowed; reading app-owned configuration/recovery records is necessary. |
| Guide: grouping in Phase 1, copy/suffixes later. Contract: single-file operations, refuse collisions. | Defer grouping, copy, suffix naming, folder moves, and general undo. Restore is specifically for Trash. |
| Guide: trash outside root, manual restore. Contract: reserved Trash inside root, built-in restore. | Use reserved top-level Trash with durable recovery metadata before removal is enabled. |
| Guide postpones logs, recovery, and tests. Contract requires them with mutation. | Tests first throughout; logging and crash recovery are mutation release gates. |
| AGENTS allows recoverable deletion by default; contract prohibits all permanent-delete features. | Apply the stricter contract: no purge, empty-trash, or automatic content removal. |
| Contract says normal operations cannot manipulate root; listing the root is required. | Clarify that root mutation is prohibited, while metadata listing/search at `.` is allowed. Empty input should be rejected explicitly. |
| Guide describes Windows 10/11 broadly; desktop release support is not defined. | Select an exact supported Windows/build/filesystem matrix before release and test it. Avoid claiming all Windows configurations are supported. |

Approval of this proposal should be followed by a targeted contract/documentation update, not retroactive claims that safeguards already exist.

## 3. Smallest useful desktop release

First deliver a read-only desktop: explicit workspace selection, listing, and filename search. Follow with folder creation, single-file move/rename, then Trash/restore. No background watcher, autostart, AI, indexing, network service, or file content processing.

Proposed initial support: a trusted local workspace on NTFS, one active root, same-volume operations, and one mutation at a time across app processes. Network shares, removable media, cloud-managed roots/placeholders, and other filesystems are outside the initial support matrix. Detect and reject unsupported cases where possible; a folder-name heuristic cannot certify that a root is not cloud managed.

This is a cooperative desktop utility, not an operating-system security sandbox. Another process with filesystem access can replace paths between checks. Canonicalization plus revalidation reduces mistakes but is not proof against malicious concurrent substitution. A stronger security claim would require a reviewed Windows handle-based implementation, stable object identity, and controlled access to ancestor directories. Do not advertise that claim for v1.

## 4. Module boundaries and dependencies

Keep the current package and tests in place. Add modules only when their milestone needs them.

| Module | Responsibility | Must not do |
| --- | --- | --- |
| `workspace.py` (existing) | Root validation; existing-path and destination-parent validation; reserved areas; Windows name/link/containment policy | Prompt, print, mutate user files, import UI |
| `operations.py` (existing) | List/search and operation planning/execution; invoke validation and recovery | Trust UI-provided absolute paths or bypass confirmation rules |
| `models.py` (new when contracts are approved) | Small data classes for entries, requests, plans, results; stable error categories | Generic plugin registry or workflow engine |
| `config.py` (new) | Persist selected root and simple preferences in app-specific local state | Silently widen the permitted root or store user contents |
| `recovery.py` (new before mutations) | Journal, Trash records, reconciliation, per-workspace writer lock, activity records | Guess a recovery outcome or purge user files |
| `ui.py` (new) | Desktop widgets, previews, explicit confirmation, progress, cancellation, error presentation | Implement filesystem safety or change files directly |
| `__main__.py` (existing) | Composition and launch; optional small diagnostic CLI | Contain business rules |

Dependency direction: entry point -> UI -> operations -> workspace/recovery. Configuration is loaded at composition; shared models have no UI dependencies. Use functions and explicit parameters before introducing service classes. A narrow callable seam for filesystem actions is justified only where needed for failure injection or platform behavior.

Do not build both a full CLI and GUI as separate products. Keep a small diagnostic entry point if it helps testing; the operation API is the reuse boundary.

## 5. Proposed contracts to freeze before parallel implementation

- Preserve `check_sandbox(Path) -> bool` as a basic predicate, with documentation that it is insufficient for authorization.
- Complete `validate_path(workspace, user_path) -> Path` for existing paths. Add separate root and destination validators rather than weakening it with many boolean switches. Destination validation checks the existing parent and a new basename; never resolve a missing destination as though it were an existing file.
- Read operations return entries with relative paths, type, and required metadata; no printing. Listing is immediate children, search is recursive filename-fragment matching with documented case behavior and stable ordering. Refused/failed traversal must be visible as partial results, not silently described as complete.
- Plans contain action, root identity, source/destination relative paths, observed file identity/metadata, and the exact changes shown to the user. Plans are immutable and single-use. Root changes invalidate pending plans.
- Execution accepts an authorized plan, acquires the writer lock, revalidates all relevant conditions, and performs only that plan. CLI/GUI confirmation is explicit; cancellation or empty input authorizes nothing. Future AI cannot manufacture user approval.
- Results distinguish success, rejected, cancelled, failed-before-change, and recovery-required/uncertain. Wrap meaningful filesystem errors at the operation boundary; never turn all exceptions into a misleading "not found".
- Existing validation exceptions remain compatible until intentionally migrated. Start with built-in exceptions; add stable operation error codes when UI presentation needs them.

## 6. Safety requirements still missing

### M0 interface agreement (documentation only)

These signatures are the handoff contract for upcoming tasks, not implemented APIs.
Keep synchronous core functions; the desktop worker provides scheduling. Avoid
parallel model/operation edits until the model owner lands these types.

```python
validate_workspace(root: Path) -> Path
validate_path(workspace: Path, user_path: str) -> Path  # existing signature
validate_destination(workspace: Path, user_path: str) -> Path
list_entries(workspace: Path, user_path: str = ".") -> ScanResult
search_names(workspace: Path, query: str, user_path: str = ".", *,
             cancelled: Callable[[], bool]) -> ScanResult
plan_operation(workspace: Path, request: OperationRequest) -> OperationPlan
execute_plan(workspace: Path, plan: OperationPlan, *,
             confirmed: bool) -> OperationResult
```

Minimal immutable data contracts:

- `Entry(relative_path: str, kind: Literal["file", "directory"])`.
- `ScanResult(entries: tuple[Entry, ...], errors: tuple[str, ...], cancelled: bool)`;
  errors or cancellation mean incomplete results. Filename matching is casefolded
  substring matching; order by casefolded relative path with original path as tie-breaker.
- `OperationRequest(action, source: str | None, destination: str | None,
  trash_id: str | None)`: action is mkdir/move/trash/restore. Move covers rename.
  Enforce required/forbidden fields by action; only relative paths accepted.
- `OperationPlan(plan_id: str, root: Path, root_identity: tuple[int, int],
  request: OperationRequest, observations: tuple[PathObservation, ...])`.
  `PathObservation` captures relative path, presence, device/file identity, size,
  modification time, type, and link count as needed; missing destinations have
  explicit absence observations and a parent identity. Record source, destination,
  and relevant parents. This is a revalidation baseline, not a race-free guarantee.
- `OperationResult(plan_id: str, status, message: str, trash_id: str | None)`;
  statuses are success/rejected/cancelled/failed/recovery_required.

The operation layer tracks issued/consumed plans and rejects foreign, modified,
stale, or reused plans. `confirmed` is supplied only by trusted application UI
code, never by future AI tool arguments; the boolean alone is not a capability or
security boundary. Mkdir requires validation/logging but no confirmation; other
mutations require true. Exact identity primitives and persistent storage schema
remain T3/T8 implementation details and must satisfy the documented contract.

M0 adopts Tkinter for the read-only prototype and SQLite for recovery when T8
starts. No toolkit/recovery feature is implemented now. GitHub hosting has not
been selected; use the local qualification runner rather than create an inert CI
workflow. Setuptools is the only declared build dependency, for standard package
metadata/discovery; source tests need no installation and no runtime packages.

### Path and object policy

Validate the absolute root and every ancestor before following redirects; reject links/junctions in the requested path and unsupported reparse types. Canonical containment must compare path components, never string prefixes. Check reserved areas on normalized/canonical identities, not just input spelling.

Test drive-relative/rooted/UNC/device paths, both separators, `..`, reserved device names, alternate data stream syntax, trailing dots/spaces, invalid/control characters, short-name aliases where enabled, case variants, long paths, and Unicode. Fail clearly on unsupported cases. Define case-only rename as refused initially. Reject mutation of non-regular objects; define and test a hard-link policy (proposed: refuse multiply-linked file mutations until supported deliberately).

Allow `.` for root listing/search only; reject empty input. Never move/rename/trash the root or app-owned state. No fallback to a broader directory if configuration becomes invalid.

### Mutation and confirmation

Existence-check-then-write is not an overwrite guarantee. Select and verify a Windows same-volume move primitive that fails if the destination exists at execution time. Do not use overwrite-enabled replacement or cross-volume copy/delete fallbacks. Bind preview to source identity and observed metadata; test replacement, destination appearance, and root changes after confirmation. Metadata checks cannot detect every content change; state that limit without reading contents.

Folder creation needs a narrow first contract: one new child beneath an existing validated parent. Nested creation can be repeated explicitly, avoiding silent partial multi-directory creation.

Locked files, permissions, disk exhaustion, long paths, and antivirus interference must produce accurate errors without retries that duplicate mutations. Prevent two app instances from mutating the same workspace concurrently. Cooperative locks do not stop external programs.

### Recovery and storage

Use reserved `Trash` for payloads and recovery metadata. Do not adopt an existing user folder of that name as app storage without explicit initialization and ownership validation. Proposed app-specific local state holds configuration/activity logs; reserve and protect any in-workspace metadata from ordinary operations. Changing configured roots must not discard access to prior recovery records.

Recommended recovery store: SQLite from the standard library, introduced only with mutation. JSON is simpler to inspect but requires custom durable update/index/reconciliation handling. SQLite transactions still cannot make filesystem moves atomic with database updates.

Minimum sequence: acquire lock -> validate plan -> durably record intent -> perform no-overwrite move -> durably record outcome -> report success. If intent cannot be saved, do not mutate. If outcome recording fails after a move, retain the payload, report recovery required, and block further conflicting mutations until reconciliation.

On restart, inspect incomplete records and actual source/destination identities. Source only, destination only, both, and neither must have explicit handling. Never automatically delete either copy or overwrite to "repair" ambiguity. Trash records retain unique ID, original relative path, root identity, timestamps, and operation state. Restore refuses occupied destinations; if original parent is missing, initially refuse and explain. Corrupt/missing metadata must not trigger cleanup or loss of payloads. Recovery is best effort after interruption, not a guarantee against hardware loss.

### Desktop and release behavior

Keep filesystem traversal off the UI thread. Start with one worker and a message queue; update widgets on the UI thread only. Serialize mutations. Cancellation stops between safe units; it cannot promise to undo a move already completed. Close/restart must preserve recoverability. Avoid content previews and automatic external launches.

Logs contain paths and therefore potentially private names: keep local, no telemetry by default, no user contents. Define activity-log rotation separately from recovery retention; rotation never discards pending recovery records or user payloads.

## 7. Technology choices and tradeoffs

| Choice | Recommendation and alternative |
| --- | --- |
| Desktop | Prototype Tkinter: low dependency cost and adequate basic lists/dialogs. Tradeoffs: styling, large-list behavior, and accessibility need testing. Consider Qt only if measured usability/accessibility gaps justify distribution and licensing review. |
| Runtime | Python 3.12+ because of junction detection. Pin a tested release for shipping; installation reports alone are not support evidence. |
| Tests | Keep unittest and TemporaryDirectory; Windows integration tests supplement mocks. No replacement framework needed. |
| Recovery | SQLite with explicit filesystem reconciliation; no ORM. Compare JSON only if scope remains demonstrably simpler. |
| Distribution | First validate a folder-based bundled executable on a clean Windows machine. A bundler adds a build dependency but avoids asking users to install Python. Choose installer/signing tooling at release time; a development install is simpler but not the desktop-product experience. |

No third-party dependency is added by this proposal. Validate current tool support when implementation begins rather than locking speculative versions now.

## 8. Verification and future AI

Use test-first development: document each test, group by function/feature, observe the intended failure, implement narrowly, then rerun affected and regression tests. Keep real Windows junction, symlink, lock, no-overwrite race, and crash-restart coverage. Skipped safety tests are gaps; release qualification requires an environment where they actually run. Use only temporary fixtures or disposable copies.

Future AI should submit typed requests to the same operation layer, receive previews, and wait for application-controlled confirmation. No shell tool, raw filesystem access, autonomous approval, or special validation bypass. This needs no model framework now.

The incremental plan and parallel ownership are in [TASKS.md](./TASKS.md).
