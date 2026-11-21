# Local File Assistant: simplified MVP

Updated 2026-11-21. This scope replaces the earlier enterprise-style roadmap and the AI-later sequencing. It is the user-authorized design, not a claim that the workflow is already implemented. See [SAFETY_CONTRACT.md](./SAFETY_CONTRACT.md) and [TASKS.md](./TASKS.md).

## Goal and workflow

A lightweight Windows application organizes a user-selected folder using local Ollama AI:

**Select folder -> Scan filenames -> AI proposes folders and moves -> User reviews -> Python executes approved operations.**

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

Keep the package layout, Python files, public validation functions, and existing tests. Do not redesign completed foundations. Configuration can store the user's chosen root; the model cannot choose or replace it. The sample sandbox constant is not the active authorization policy.

Last recorded T6 run (2026-11-21): 146 tests, 144 passed, two real-symlink tests skipped due to session privileges; real junction tests passed. All 24 T13 tests passed using fake transport. Scanning, the proposal client and review planning are implemented; the approval UI, journal and file moves remain future tasks. This is historical evidence, not deployment qualification.

## Minimal components

| Component | Responsibility |
| --- | --- |
| [workspace.py](./file_assistant/workspace.py) | Shared validation for root, existing files, and future destinations. |
| [config.py](./file_assistant/config.py) | Persist trusted user selection; keep app state separate from managed files. |
| [models.py](./file_assistant/models.py) | Existing entries, requests, observations, plans and results; `OrganizationPlan` binds an immutable batch of operation plans to its root and scan. |
| [operations.py](./file_assistant/operations.py) | Implemented: metadata-only scans, listing, filename search, manifests and T6 review planning. Execution remains T9. |
| [ollama_client.py](./file_assistant/ollama_client.py) | Implemented: send a bounded filename manifest to local Ollama; parse and validate the proposal format. No filesystem execution. |
| `ui.py` (planned) | Folder picker, scan list, proposed changes, explicit approval, progress, and results. |
| `recovery.py` (planned) | Small durable move journal and interrupted-run inspection. No generic workflow engine. |
| [__main__.py](./file_assistant/__main__.py) | Wire the components and start the UI. |

The UI calls Python operations. Operations enforce safety independently of the UI and AI. The Ollama client receives an in-memory manifest; it never receives filesystem capabilities. Use a single background worker for scan/inference/execution so the UI stays responsive, and update Tkinter widgets on the UI thread.

Use Tkinter, the standard library, and the existing unittest setup. A small local HTTP client is sufficient for Ollama; no agent framework, MCP tools, plugin system, ORM, or dependency-injection framework. Keep existing dataclasses, but do not expand a generic planning system ahead of need.

## AI boundary and proposal format

The application creates a scan ID and assigns each eligible file an opaque ID. Send only that ID, filename, and extension, plus existing destination folder names if needed. Do not send absolute paths, user-file bytes, configuration, journal data, or security metadata. Treat filenames as untrusted data, including names that look like instructions.

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

AI proposals never contain an authoritative root, plan ID, observation record, or confirmation flag. Python maps file IDs back to the captured scan and builds existing OperationRequest/OperationPlan objects. The small OrganizationPlan aggregate contains those plans rather than duplicating them. The planned UI will display the exact relative source/destination for every move and new folder. Deselecting moves removes their unneeded folder creations. Any edit regenerates the plan and requires fresh review.

Implemented planning boundary: `build_organization_plan(workspace, snapshot, proposal, *, proposal_scan_id, selected_file_ids=None)` returns a fresh immutable `OrganizationPlan`. The caller retains the scan ID at inference dispatch and supplies it with the current scan/root; model output cannot supply this context. Both JSON and dictionary proposals undergo T13 parsing. Planning requires complete successful scan evidence, revalidates root/source/existing-parent identity and metadata, validates the whole proposed batch before selection, preserves basenames and uses T3's explicitly planned-folder validation. New-folder observations record absence without inventing a future parent identity. These checks perform no managed writes and cannot replace T9 execution-time revalidation or T11's approval lifecycle.

## Execution and essential recovery

On explicit approval, Python freezes the selected plan, binds it to the chosen root and scan, prevalidates the whole batch, then performs one operation at a time. Revalidate source identity/metadata, destination parent, and root immediately before each operation. Changing the selected folder, rescanning, or receiving another proposal invalidates prior approval. Do not reuse approvals or replay completed plans.

No overwrite, including a destination created after preview. Use a verified same-volume no-replace move primitive; never silently fall back to copy/delete or choose a new name. Preserve basenames; refuse multi-hard-linked sources, redirects, and unsupported objects. Check collisions across the entire proposed batch, including case-insensitive names and file/folder conflicts.

A batch is not an atomic filesystem transaction. Stop on failure or cancellation between moves; show completed, failed, and not-started operations. Do not promise rollback. Completed moves remain in place and must be recorded.

Use a small app-local JSON journal initially, rather than the previously proposed SQLite subsystem. Record intent durably before each move and outcome afterward using atomic journal replacement. If recording intent fails, do not move. If recording the outcome fails after a move, report uncertainty and block further execution. Record root identity, original and destination relative paths, observed identity, and status. On restart, inspect unfinished records without reading contents. Show identifiable outcomes; ambiguous/missing state requires user resolution and must never trigger automatic deletion, overwrite, or retry. Empty created folders may remain after failure; report them rather than automatically removing them.

Allow only one executing app instance through a simple OS-released lock. A global app mutation lock is sufficient for this MVP; no multi-user coordination service. Persist journals outside managed roots and do not remove unfinished records automatically. There is no automatic undo feature in this release; the move record supports user recovery.

## Security limits and deployment

Python allowlists and validators are the enforcement boundary for this app. The local model is only a text generator: no filesystem tools, shell, Python execution, autonomous loop, or self-approval. This does not claim OS isolation from a separately compromised Ollama process or another program running as the user. Concurrent hostile path replacement remains an explicit limitation; never describe validation alone as a security sandbox.

Initial supported target: Windows 11, Python >=3.12, local fixed NTFS folder. Reject detected unsupported/reparse storage and document that drive-letter syntax alone does not prove NTFS, fixed storage, or absence of cloud synchronization. Add a small storage preflight before enabling execution; no broad filesystem compatibility layer.

Deployment first means a documented local installation: Python, Ollama, one tested local model, install/launch commands, and a disposable-folder smoke test. Ollama setup/model download is an explicit user prerequisite; organization runs locally afterward. Test the actual selected model, not a broad model matrix. A bundled executable can follow an explicit request; an installer, code signing, enterprise telemetry, CI fleet, and automatic updater are not MVP dependencies.

The README and older guide may still describe AI-later sequencing until the deployment-documentation task updates them. This architecture and the safety contract govern the new scope; do not delete completed safety work to match older prose.
