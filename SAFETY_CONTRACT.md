# Safety contract: filename-only local AI organizer

Updated 2026-11-20 for the simplified MVP. These are required behaviors, not claims that all features exist. This replaces earlier AI-later sequencing and permits reviewed batches of file moves. Completed validation and configuration protections remain in force.

## 1. User-selected boundary

- One existing folder is explicitly selected by the user and validated by Python. Configuration changes are trusted UI actions, never AI proposals.
- Keep app configuration and journals outside managed content. Refuse overlapping managed roots and app-state directories.
- Never move, rename, delete, or replace the root. Never expand scope automatically after an invalid selection or configuration error.
- MVP scans only regular files directly in that folder. Moves preserve filenames and stay within the same root and volume, targeting one-level subfolders. Existing subfolders are not recursively reorganized.
- Refuse absolute/rooted/drive-relative paths, traversal, invalid Windows names, alternate data streams, reserved names, symlinks, junctions, and unsupported reparse points. Validate root ancestors and resolved containment using path components.
- Keep top-level Trash reserved and excluded. Do not adopt existing user directories as app state.
- Target local fixed NTFS storage on Windows 11. Reject detected unsupported locations; do not claim that path syntax alone proves storage suitability.

## 2. No user-file content reads

- Scan directory entries for filenames/extensions only. Never read user-file bytes, compute hashes, extract metadata from file contents, render previews, or index contents.
- Filesystem metadata such as type, identity, size, timestamps, link count, and volume may be inspected for safety, not sent to AI for classification.
- Reading/writing app-owned configuration and execution journals is allowed. Tests may create/read disposable fixture files to verify preservation.
- Never launch user files, use associated applications, or execute a file suggested by the model.

## 3. AI proposes only

- Send only opaque file IDs, filenames/extensions, and necessary destination folder names to a local Ollama model.
- AI has no filesystem tools, shell, arbitrary Python execution, configuration editing, approval ability, or execution API.
- The only accepted proposal is a bounded structured list of file IDs and single-component destination folder names. Python supplies the root, sources, plan identity, observations, and creation steps.
- Reject unknown fields/actions, malformed types/JSON, duplicate or unknown IDs, unsafe names, and oversized responses. Never execute generated code or interpret filenames as instructions.
- Use a loopback endpoint without remote redirects/proxy forwarding or cloud fallback. No automatic model downloads or app-launched shell setup commands.
- Model failure, unavailability, or invalid output changes no managed files.

## 4. Review and authorization

- Scanning and requesting a proposal do not change managed files or create destination folders.
- Show every source, destination, and new folder before approval. User approval is explicit; cancel, empty response, or closing the review executes nothing.
- Folder creation for organization is part of the approved plan; it is not performed immediately on AI suggestion.
- User can reject/deselect moves. The executable plan must exactly match the reviewed subset; unneeded folder creations are removed.
- Root changes, rescans, plan edits, or new proposals invalidate approval. AI cannot set the approval flag. Do not replay an already executed plan.
- Revalidate the whole batch before starting and relevant paths/identities immediately before each operation. Stale source or destination conditions require a new review.

## 5. File preservation

- Never overwrite, including conflicts appearing after review. No collision suffixes, fallback replacement, copy/delete fallback, or filename changes.
- Reject duplicate sources/destinations, case-insensitive collisions, file/folder conflicts, multiply-hard-linked source files, and unsupported objects.
- Only approved folder creation and same-volume moves are executable in the MVP. No deletion, Trash/restore operation, purge, folder moves, or automatic cleanup of user files.
- Preserve existing models and reserved Trash checks; unsupported action values must still be rejected by the MVP executor even if a general model type can represent them.
- Handle locks, missing files, permission failures, disk failures, and cancellation accurately. A batch is sequential, not all-or-nothing. Stop on the first unexpected failure and report completed and untouched items.

## 6. Minimal execution record

- Before a move, persist intent in app-local state; afterward persist its outcome. If intent cannot be saved, do not execute. If outcome recording fails, stop and report recovery required.
- Record original/destination relative paths, root identity, observed file identity, and status. No file contents in records; filenames are private data, kept local with no telemetry.
- After interruption, inspect incomplete operations and report actual or uncertain outcomes. Never delete, overwrite, or blindly repeat a move to repair ambiguity.
- A simple app-wide mutation lock prevents concurrent app instances executing batches. It does not lock out unrelated software.
- Retain unresolved records. There is no automatic rollback or general undo; completed moves and any empty newly created folders may remain and must be shown to the user.

## 7. Verification and limits

Tests precede implementation, use only disposable fixtures, and remain grouped/commented by behavior. Preserve T0/T1/T2/T4 code and tests. Add focused integration coverage for config-to-validator use, no content reads, hostile filenames/proposals, approval, collisions, cancellation, interrupted moves, and journal failures. Real Windows junction/symlink behavior must be exercised in a suitable environment before deployment; mocked behavior is not equivalent.

The utility prevents unauthorized operations through its own interface. It does not sandbox the operating system or guarantee protection against malicious concurrent path substitution, compromised external processes, or hardware loss. Python validation must be called at the operation boundary; model instructions alone never enforce safety.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the minimal design and [TASKS.md](./TASKS.md) for remaining work.
