# Local File Assistant: simplified MVP

Updated 2026-11-23. This scope replaces the earlier enterprise-style roadmap and the AI-later sequencing. T11 is implemented in the `feat/T11-chat` working tree, pending review/integration; deployment qualification is still separate. See [SAFETY_CONTRACT.md](./SAFETY_CONTRACT.md), [UX.md](./UX.md) and [TASKS.md](./TASKS.md).

## Goal and workflow

A lightweight conversational Windows application organizes a user-selected folder using local Ollama AI. Chat is primary; folder selection and Ollama status are secondary. [UX.md](./UX.md) defines conversation states, refinement, inline review cards and application-controlled confirmation:

**Select folder -> Describe request -> Scan filenames -> Discuss/refine proposal -> Review move cards -> Explicitly confirm -> Python executes approved operations.**

Only filenames and extensions inform organization. Never read user-file contents: no text extraction, hashing, thumbnails, document parsing, embeddings, or content indexing. Filesystem metadata may be inspected solely for validation and change detection. Reading the application's own configuration and move records is permitted.

Keep the initial scope small: scan regular files directly inside the selected folder, preserve each filename, and move them into one-level subfolders within that same root. Existing safe subfolders may be reused; new ones are created only as part of an approved plan. Do not recursively reorganize existing subfolders. This bounds both the AI prompt and recovery work; recursive organization can be a later explicit feature.

No delete/trash/restore UI, file launching, standalone rename, copy, cross-volume move, background watching, general-purpose agent, or cloud inference in the MVP. Preserve existing Trash exclusions and model types for compatibility; their presence does not enable those actions.

## Preserve completed work

- **T0:** safety/design decisions; this revision intentionally changes product sequencing and reduces scope.
- **T1:** Python >=3.12 metadata, Windows baseline, and strict qualification runner. The earlier manual baseline passed 15 tests without skips.
- **T2:** existing-root/path validation, Windows names, ancestor link/junction checks, canonical containment, and tests.
- **T3:** destination validation for existing parents and one explicitly planned new root child, without creating folders.
- **T4:** immutable data classes and atomic configuration persistence, now integrated with T2 and rejecting overlap with app state. Request parsing rejects rooted paths and malformed fields.
- **T5:** bounded read-only scans, listing, nonrecursive filename search, and inference manifests with local metadata snapshots.
- **T13:** local Ollama HTTP client with strict proposal parsing, local-model checks, loopback-only requests, payload bounds and fake-transport tests. Live-model verification is recorded in [TASKS.md](./TASKS.md).
- **T6:** immutable review planning bound to the trusted root and scan, strict proposal validation, metadata revalidation, collision checks and subset selection. Derives required folders without creating them; carries no approval or execution capability.
- **T8:** strict app-local JSON journals, a Windows process execution lock and metadata-only inspection of unfinished records. Reuses T6 plans and observations; performs no managed filesystem operations.

Keep the package layout, Python files, public validation functions, and existing tests. Do not redesign completed foundations. Configuration can store the user's chosen root; the model cannot choose or replace it. The sample sandbox constant is not the active authorization policy.

T9 and journal completion are implemented in `99b27ee` and reviewed on 2026-11-22: 246 tests ran, 244 passed, two real-symlink tests skipped due to session privileges. Real Windows moves and cancellation/recovery tests passed. T13 conversation support is implemented and reviewed in `01e3753`; its synthetic two-turn local-model smoke passed. T11's Tkinter checkpoint is `e3d3492`; the user subsequently requested PySide6 (Qt) for clearer UI code. The current Qt workflow and stylesheet replace that renderer without changing the controller or backend. Automated and scripted native GUI checks are recorded in UX.md. Live-model desktop and end-to-end deployment qualification remain pending.

## Minimal components

| Component | Responsibility |
| --- | --- |
| [workspace.py](./file_assistant/workspace.py) | Shared validation for root, existing files, and future destinations. |
| [config.py](./file_assistant/config.py) | Persist trusted user selection; keep app state separate from managed files. |
| [models.py](./file_assistant/models.py) | Existing entries, requests, observations, plans and results; `OrganizationPlan` binds an immutable batch of operation plans to its root and scan. |
| [operations.py](./file_assistant/operations.py) | Implemented: metadata-only scans, listing, filename search, manifests and T6 review planning. T9 implements explicit app-owned approval, fixed-NTFS preflight and journaled no-replace execution. |
| [ollama_client.py](./file_assistant/ollama_client.py) | Implemented: send a bounded filename manifest to local Ollama; parse and validate the proposal format. No filesystem execution. |
| [ui/](./file_assistant/ui/) | Implemented T11 chat, request/refinement context, inline T6 review cards, app-controlled confirmation, actual results and recovery review; secondary folder/model controls. |
| [recovery.py](./file_assistant/recovery.py) | Implemented: strict JSON batch journals, Windows app-wide execution lock and metadata-only interrupted-run inspection. No executor or automatic repair. |
| [__main__.py](./file_assistant/__main__.py) | Wire the components and start the UI. |

The UI calls Python operations. Operations enforce safety independently of the UI and AI. The Ollama client receives an in-memory manifest; it never receives filesystem capabilities. A single background worker handles scan/inference/planning/execution/recovery I/O; a Qt timer polls completion results and updates widgets on the GUI thread.

Use PySide6 Qt Widgets for the UI, the standard library for the existing backend/worker, and the existing unittest setup. The user-authorized UI migration replaces the original Tkinter choice. `PySide6-Essentials>=6.12,<7` supplies QtCore, QtGui and QtWidgets (plus its matching Shiboken binding runtime); the full PySide6 meta-package and its unused add-ons are unnecessary. See [Qt's package details](https://doc.qt.io/qtforpython-6/package_details.html). A small local HTTP client remains sufficient for Ollama; no agent framework, MCP tools, plugin system, ORM or dependency-injection framework. Preserve the existing dataclasses and safety boundaries.

## UI package boundaries

The user-requested refactor replaces the single `ui.py` with a package grouped by purpose. Existing imports from `file_assistant.ui` and the `python -m file_assistant` launch command remain valid.

| Module | Responsibility |
| --- | --- |
| [ui/__init__.py](./file_assistant/ui/__init__.py) | Public UI imports, `QApplication` composition and `main()` launch. |
| [ui/controller.py](./file_assistant/ui/controller.py) | Conversation state/history, root/scan/request revisions, review cards, selection, exact confirmation, invalidation, cancellation and close lifecycle. No widget rendering. |
| [ui/desktop.py](./file_assistant/ui/desktop.py) | Qt Widgets layout, inert plain text, review/confirmation controls, GUI-thread polling with `QTimer`, and safe close events. |
| [ui/services.py](./file_assistant/ui/services.py) | Thin adapters over existing configuration, validation, scanning, Ollama, planner, approval, executor and recovery APIs. Called on the worker for I/O; approval issuance is an in-memory UI action. |
| [ui/worker.py](./file_assistant/ui/worker.py) | One background worker with queued results; callbacks run only when polled by the UI thread. |
| [ui/formatting.py](./file_assistant/ui/formatting.py) | Inert text summaries from Python-owned plans, execution reports and recovery evidence. |
| [ui/theme.py](./file_assistant/ui/theme.py) | Installed-font selection and loading the packaged Qt stylesheet. No backend authority or custom painting. |
| [ui/theme.qss](./file_assistant/ui/theme.qss) | Qt stylesheet for colors, fonts, rounded borders, focus and disabled controls. Included as package data in `pyproject.toml`. |

The controller uses services and text formatting without constructing widgets. The desktop renders controller-owned state. Qt draws rounded surfaces from stylesheet rules; there is no manual raster/canvas rounding code. Model content is always plain text, never rich text, links or generated authorization controls. Backend validation and execution boundaries remain unchanged. See UX.md for the visual design, exact controls and verification limits.

## AI boundary and proposal format

The implemented T13 extension accepts bounded organization messages/follow-ups and a strict conversational reply envelope as specified in UX.md. Clarifications carry no moves; model prose is inert. T11 extracts the existing move proposal for T6, retains trusted request/scan identities, and invalidates stale cards and approval. No agent framework or planner rewrite is needed.

The application creates a scan ID and assigns each eligible file an opaque ID. Send only that ID, filename, extension and needed destination folder names, plus bounded organization conversation context through the T13 conversation interface. Do not send absolute paths, user-file bytes, configuration, journal data, or security metadata. Treat filenames as untrusted data, including names that look like instructions.

The initial proposal format is deliberately narrow:

```json
{
  "moves": [
    {"file_id": "f1", "destination_folder": "Documents"}
  ]
}
```

Destination folder names are single validated Windows components, not arbitrary paths. Omitted files remain unchanged. An empty move list is a valid no-op. Python derives missing folders from approved moves; there is no separate AI command to create arbitrary directory trees.

Reject malformed JSON, unknown keys, wrong types, unknown/duplicate file IDs, excessive output, unsafe folder names, reserved names, and any extra action such as shell, delete, root selection, or approval. Use a strict parser and explicit checks, never eval or generated Python. Structured-output support improves reliability but is not authorization; validate every response regardless of model claims.

Use only a loopback Ollama endpoint and a local model explicitly selected by the user. Do not follow redirects or system proxies to a remote service. No cloud fallback, automatic model download, or shell invocation from the app. Apply finite request timeouts, response-size limits, and a documented manifest/file-count limit; an oversized folder must produce a clear message rather than a silently incomplete plan. Verify exact Ollama API/model compatibility during implementation.

AI proposals never contain an authoritative root, plan ID, observation record, or confirmation flag. Python maps file IDs back to the captured scan and builds existing OperationRequest/OperationPlan objects. The small OrganizationPlan aggregate contains those plans rather than duplicating them. The UI displays exact relative source/destination paths and required new folders from the plan. Deselecting moves removes their unneeded folder creations. Any edit regenerates the plan and requires fresh review.

Implemented planning boundary: `build_organization_plan(workspace, snapshot, proposal, *, proposal_scan_id, selected_file_ids=None)` returns a fresh immutable `OrganizationPlan`. The caller retains the scan ID at inference dispatch and supplies it with the current scan/root; model output cannot supply this context. Both JSON and dictionary proposals undergo T13 parsing. Planning requires complete successful scan evidence, revalidates root/source/existing-parent identity and metadata, validates the whole proposed batch before selection, preserves basenames and uses T3's explicitly planned-folder validation. New-folder observations record absence without inventing a future parent identity. These checks perform no managed writes and cannot replace T9 execution-time revalidation or T11's approval lifecycle.

## Execution and essential recovery

On explicit approval, Python freezes the selected plan, binds it to the chosen root and scan, prevalidates the whole batch, then performs one operation at a time. Revalidate source identity/metadata, destination parent, and root immediately before each operation. Changing the selected folder, rescanning, or receiving another proposal invalidates prior approval. Do not reuse approvals or replay completed plans.

No overwrite, including a destination created after preview. Use a verified same-volume no-replace move primitive; never silently fall back to copy/delete or choose a new name. Preserve basenames; refuse multi-hard-linked sources, redirects, and unsupported objects. Check collisions across the entire proposed batch, including case-insensitive names and file/folder conflicts.

A batch is not an atomic filesystem transaction. Stop on failure or cancellation between moves; show completed, failed, and not-started operations. Do not promise rollback. Completed moves remain in place and must be recorded.

Use a small app-local JSON journal initially, rather than the previously proposed SQLite subsystem. Record intent durably before each move and outcome afterward using atomic journal replacement. If recording intent fails, do not move. If recording the outcome fails after a move, report uncertainty and block further execution. Record root identity, original and destination relative paths, observed identity, and status. On restart, inspect unfinished records without reading contents. Show identifiable outcomes; ambiguous/missing state requires user resolution and must never trigger automatic deletion, overwrite, or retry. Empty created folders may remain after failure; report them rather than automatically removing them.

Allow only one executing app instance through a simple OS-released lock. A global app mutation lock is sufficient for this MVP; no multi-user coordination service. Persist journals outside managed roots and do not remove unfinished records automatically. There is no automatic undo feature in this release; the move record supports user recovery.

Implemented T8 boundary: `ExecutionLock(workspace, state_directory=None)` uses a nonblocking Windows byte-range lock in the existing app-state directory. All instances must use the same trusted directory. The lock file remains after release; its presence alone never implies ownership. T9 must hold the lock across journal/recovery checks, `begin_batch`, `record_intent`, execution and `record_outcome`. A move into a newly created folder must carry the parent identity recorded by the preceding successful mkdir outcome. Persistence failure stops the current session; failed outcome publication preserves the last durable intent. Corrupt/unresolved journals block new batches, retained batch IDs prevent replay, and a new lock session cannot automatically resume an interrupted batch. `inspect_recovery` reports source-only, destination-only, ambiguous or unsafe states from recorded identities and live metadata; it never changes files or resolves the record automatically.

Journal updates flush/fsync a same-directory temporary file, close it and atomically replace the batch JSON. This prevents partially published JSON during ordinary process interruption, but does not guarantee directory-metadata persistence on Windows or immunity to hardware caching/power loss. Completed and unresolved journals are retained; bounds are 8 MiB per file, 2,000 operations per batch and 1,000 journals. Limits fail explicitly; cleanup, automatic resolution and rollback are not provided. Storage preflight, approval enforcement and actual no-overwrite operations remain T9 responsibilities.

Implemented T9 completion: known unsubmitted operations can be retained as `not_executed`; uncertain attempts remain blocking. Explicit `resolve_recovery` acknowledgement compares the exact journal and fresh metadata findings, preserves the original evidence and permits a new plan. It never resumes, repairs or retries the old batch. T11 must collect this acknowledgement separately from move approval; model text cannot supply either.

## Security limits and deployment

Python allowlists and validators are the enforcement boundary for this app. The local model is only a text generator: no filesystem tools, shell, Python execution, autonomous loop, or self-approval. This does not claim OS isolation from a separately compromised Ollama process or another program running as the user. Concurrent hostile path replacement remains an explicit limitation; never describe validation alone as a security sandbox.

Initial supported target: Windows 11, Python >=3.12, local fixed NTFS folder. Reject detected unsupported/reparse storage and document that drive-letter syntax alone does not prove NTFS, fixed storage, or absence of cloud synchronization. T9 implements a small fixed-NTFS storage preflight before execution; no broad filesystem compatibility layer.

Deployment first means a documented local installation: Python, Ollama, one tested local model, install/launch commands, and a disposable-folder smoke test. Ollama setup/model download is an explicit user prerequisite; organization runs locally afterward. Test the actual selected model, not a broad model matrix. A bundled executable can follow an explicit request; an installer, code signing, enterprise telemetry, CI fleet, and automatic updater are not MVP dependencies.

The README and older guide may still describe AI-later sequencing until the deployment-documentation task updates them. This architecture and the safety contract govern the new scope; do not delete completed safety work to match older prose.
