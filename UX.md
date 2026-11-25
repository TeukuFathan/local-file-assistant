# Conversational local file assistant UX

Updated 2026-11-23. Desktop UX specification and current T11 implementation notes. The Tkinter version was checkpointed in `e3d3492` before the user-requested migration to PySide6 (Qt). The Qt UI is now implemented in the `feat/T11-chat` working tree, pending review and integration. Deployment qualification remains T12 work.

## Product and scope

Chat is the primary interface. Users describe how they want the selected folder organized, receive conversational replies from local Ollama, and refine suggestions through follow-up messages. Proposed moves appear as reviewable cards in the conversation. Python validates plans; only an explicit application-controlled confirmation can start execution.

The workflow is: **select folder -> describe request -> scan filenames -> discuss proposal -> review cards -> explicitly confirm -> see actual results**.

This user-approved UX replaces the earlier form-oriented T11 presentation. The safety boundaries and backend responsibilities in TASKS.md and ARCHITECTURE.md still apply. The T13 conversation extension (`01e3753`) preserves the original moves-only API. T11 now connects it to desktop chat, review cards, explicit confirmation and execution/recovery reporting. TASKS.md records verification and remaining work.

The MVP still organizes only regular files directly inside one user-selected folder, using filenames and extensions. Preserve basenames and move only into one-level child folders under that root. No file-content reads, previews, attachments, recursive organization, deletion, arbitrary rename, file launching, shell commands, cloud inference, autonomous tools, or general-purpose agent behavior.

## Screen and interaction

The main area is a scrollable conversation with a bottom text composer and Send button. Enter in the composer sends a message; Shift+Enter inserts a new line. No button is a default Enter/Return action, so Enter never confirms file operations. Messages, proposal cards, confirmation controls and execution reports are visually distinguishable.

A persistent context panel shows the selected folder, the folder-picker and Developer details controls, a Local model text field with an optional Change model action, Inspect / rescan, and Stop. Per the user's choice, `qwen3.5:9b` is active by default; no model-selection click or prompt is required before sending a request. An already selected alternative model is displayed instead, and Change model remains available as an optional override. Startup and folder selection do not contact Ollama, load or download models, or authorize file changes. The existing local-model check runs before filenames are sent. The status line shows conversation state, the active model and latest request status; it is not a continuously polled Ollama health indicator.

These are secondary controls, but the active folder remains visible during review and confirmation. The model cannot select or change it. Selecting a folder validates and saves the choice and inspects recovery records; it does not send filenames to Ollama. A submitted request scans filenames when a fresh scan is needed. Follow-ups reuse the current complete scan. An unsent request remains local in the composer; selecting a folder does not submit it automatically. Each app launch starts without an active folder and with the user-authorized `qwen3.5:9b` default, even though the earlier folder choice is saved in app configuration.

Without a validated folder, the picker says Choose folder and uses a light-blue background (`#DBEAFE`), blue text and a blue border to invite the first action. Cancellation or an invalid selection leaves that appearance in place. After a valid folder is selected, it says Change folder and returns to the quiet button style. Existing disabled-state behavior still applies while executing or closing.

Initial guidance: "Choose a folder, then tell me how you want its files organized. I use filenames only. Nothing moves until you review and confirm."

Example conversation:

1. User: "Group my photos and spreadsheets into separate folders."
2. Assistant: "I suggest Pictures and Spreadsheets. Review the proposed moves below."
3. Cards show `holiday.jpg -> Pictures/holiday.jpg` and `budget.xlsx -> Spreadsheets/budget.xlsx`, with both new folders identified.
4. User: "Call the spreadsheet folder Finance, and leave the photos where they are."
5. The earlier proposal is marked Superseded. A fresh validated card shows only `budget.xlsx -> Finance/budget.xlsx`.
6. The user selects Review and apply, checks the exact summary, then clicks the application-owned Confirm moves button.
7. An application result message reports what actually happened. The assistant never claims completion based on its own prediction.

Ambiguous requests may receive a brief clarification question. Unsupported requests receive a concise scope explanation, without executing a partial interpretation. A reply without proposed moves creates no actionable card.

## Visual design

The implemented interface uses a light background, white rounded surfaces, subtle borders and indigo action buttons. The header presents the application title and the subtitle "Organize by filename. You stay in control." Chat, review, confirmation and recovery content appear in separate rounded panels with explicit role or purpose labels. User messages have a pale indigo background. Color supports these labels; it does not grant approval or indicate execution success by itself.

| Element | Current treatment |
| --- | --- |
| Background and surfaces | Background `#F4F6FB`, white surfaces, borders `#E3E8F0`. |
| Text | Main text `#172033`, secondary text `#667085`; proportional sans-serif text in messages, paths, inputs and controls. |
| Actions | Indigo `#5146D8` for Send, Review and apply, Confirm moves and Confirm acknowledgement; lighter secondary and quiet button styles. Disabled controls have a separate muted appearance. |
| Shape and spacing | Qt stylesheet radii of 16 logical pixels for panels and 10 for buttons, typically 18 pixels of panel padding and 28 pixels of outer horizontal spacing. Qt draws the shapes. |
| Typography | Title 23 points bold, body/composer 11–12 points, action labels 10 points semibold, secondary text 9–10 points. |
| Fonts | Prefer installed Inter, then Inter Variable, Segoe UI Variable Text, Segoe UI, Arial, then the existing system default. Fonts are not bundled, installed or downloaded. The Windows Qt smoke used Segoe UI Variable Text because Inter was absent. |

The UI uses PySide6 Qt Widgets: `QFrame` panels, `QPushButton` controls, `QLineEdit` model input and `QPlainTextEdit` messages/composer. `theme.qss` holds the colors, rounded borders, focus and disabled-state styling; `theme.py` selects an installed font and loads that packaged stylesheet. Qt replaces the previous custom canvas/raster rounding code. The user-approved `PySide6-Essentials>=6.12,<7` runtime dependency supplies the required Qt modules; unused WebEngine/PDF/add-on packages are not required. There are no downloaded fonts or artwork. Widget construction and rendering remain in `desktop.py`.

Messages and exact paths are selectable, copyable, inert plain text, never rich text or links. They wrap within their panel, and long text has a vertical scrollbar; a shortened label is never the only way to inspect a move. The conversation scrolls to the newest content after layout so confirmation controls are reachable. The window currently opens at 1080 × 850 with an 820 × 650 minimum. Offscreen tests cover confirmation visibility at the minimum size and full long/Unicode path text; a broader DPI/window-size and accessibility matrix remains pending.

## Developer details

Developer details opens a nonmodal, read-only window. A request selector identifies each captured conversation request by a distinct request number, model and user-text preview. The full selectable text includes the user request, captured revision/scan identity, model, elapsed time and validation/network/application errors, including chained timeout causes. Elapsed request time includes preparation and planning as well as inference.

For each existing `/api/show` and `/api/chat` exchange, display the exact JSON body passed to the transport and whether sending completed, HTTP status, and the received raw body before JSON validation. Also show raw assistant content, including malformed JSON, and the validated move proposal when available. A clarification has no actionable proposal. A client-validated proposal is explicitly labelled as not being a T6 plan or approval; later planning failures remain visible alongside it. Diagnostics for cancelled or superseded requests keep their original identities and never reactivate cards.

All display is inert plain text. Copy copies the selected request's displayed details to the clipboard only when clicked. Clear removes all retained diagnostics without changing conversation history, cards, confirmation, approval, journals or files. It does not erase previously copied clipboard data. Closing or pressing Escape releases the panel's cached text but keeps the bounded store available for reopening. Clearing or evicting an in-flight request prevents later events from restoring its diagnostic entry.

Retention is process-local and fixed at the latest four requests, each with at most the two existing HTTP exchanges. Per-field UTF-8 display limits are 512 KiB per JSON request, 256 KiB per HTTP body or assistant content, 512 KiB per validated proposal, 4 KiB per error, 8 KiB for user request text, and 128 bytes each for model/scan labels. Clipped fields are explicitly identified. Invalid UTF-8 bytes are escaped; incomplete reads are labelled as received prefixes. A status/header rejection before the existing transport reads a body, or a timeout before bytes arrive, shows that no body was captured. The app does not reread a response or relax transport checks for diagnostics.

There are no automatic disk logs, persistent diagnostic history, file-content reads or extra model calls. Diagnostics are never sent back to Ollama. Capture, formatting, initialization, display and clipboard failures are isolated from the normal workflow. The panel receives only the diagnostic store and exposes no execution, approval, retry or recovery action.

## Review cards and confirmation

Each proposal is a versioned card group rendered from the T6 plan, not from model prose or model-supplied paths. Each move card shows:

- Exact source and destination relative paths, preserving the filename.
- A selected/deselected control.
- Whether its destination folder already exists or will be created.

Show a batch summary with selected move count and all required new folders. Long names must be inspectable in full without opening the file; do not rely on a truncated label for review. Render filenames and model text as inert text, never executable markup or file links. Do not expose IDs or filesystem identity metadata as normal user controls.

Deselecting a move rebuilds the T6 subset plan and removes unnecessary creations. Follow-up messages can change grouping or destination folder names within the existing scope. Every revision produces a fresh plan identity and invalidates any earlier confirmation. Old cards remain visible as Superseded, with their action controls disabled. Only one proposal is active.

Review and apply opens an application-controlled confirmation panel showing the current folder, exact selected moves and required folder creations. Confirm moves explicitly approves that immutable plan once and hands it to T9. Cancel returns to review without execution. Disable apply for empty, invalid, stale, superseded, already submitted or recovery-blocked plans. Immediately disable repeat submissions after confirmation.

Typed "yes", "apply", "go ahead", model approval fields, and assistant prose never mint approval. Typed text can be submitted as a conversational follow-up and supersede the current proposal; it cannot invoke confirmation. The app's confirmation controls cannot be generated by model text. Changing the selected folder or model, rescanning, revising the proposal or changing the selection closes any pending confirmation and invalidates its approval.

## Minimum conversation states

These are UI states, not a new workflow framework. The implementation identifiers are shown in parentheses. Model request status is separate, so a failed request does not hide existing results or recovery information.

| State | Conversation behavior | Backend connection and permitted transition |
| --- | --- | --- |
| Awaiting folder (`no_folder`) | Explain scope; retain an unsent request locally; highlight Choose folder in light blue. | Trusted folder picker/configuration and validators. No proposal or execution. Valid selection with no recovery blocker enters Ready. |
| Ready (`ready`) | Accept an organization request; show active folder and model. | T11 uses the complete current scan or dispatches T5 scanning on submission, using the user-authorized default or an optional explicit override. The backend still verifies the local model before sending filenames. |
| Preparing (`thinking`) | Show working/request status; allow cancellation; prevent overlapping submissions. | T5 must finish completely before T13. Capture root, scan ID and request/revision identity locally. Valid reply enters Clarifying or Reviewing; error enters Blocked. Also used while validating selection, rebuilding a subset or doing recovery I/O. |
| Clarifying (`clarification`) | Display a short model question and accept a follow-up. No apply control. | Bounded T13 conversation context. A subsequent reply may produce a fresh proposal. |
| Reviewing (`reviewable`) | Display model explanation plus Python-validated move cards; accept refinement/deselection. | T13 proposals pass T6 before cards become actionable. Refinement returns to Preparing; Review and apply opens confirmation. An empty subset enables no apply action. |
| Confirming (`confirming`) | Show the exact application-owned summary with Confirm moves and Cancel. | Only confirmation of the current immutable plan/root/scan issues one-use approval and starts T9. Cancel returns to review. Context or selection changes invalidate this panel. |
| Executing (`applying`) | Show applying status and Stop; disable changes to folder, model, proposal, selection and approval. | T9 uses T8 journaling/locking. There is no live per-operation progress counter; actual operation results arrive when the worker returns. Cancellation stops at supported operation boundaries; never promise rollback. |
| Finished (`finished`) | Show completed, stopped and not-started operations, including created folders. | Render T9 results, not model claims. Clear history and scan; the next request requires fresh scan context. A recovery-required result enters Blocked. |
| Blocked (`blocked`) | Explain invalid selection, failed/incomplete scan, stale plan, unavailable model, malformed response or recovery requirement. | Retry only through the relevant explicit user action. Recovery blockers disable new organization requests and execution until safely resolved; restarting chat does not clear journals. |

Cancelling inference invalidates its request identity immediately; discard late results even if the HTTP call cannot yet be interrupted. Keep the single worker busy until it finishes rather than starting another inference concurrently. Changing folder or starting a new scan invalidates all earlier in-flight responses and actionable cards. A late result must not replace a newer review or reuse recurring file IDs from another scan.

Inspect / rescan clears the previous scan, conversation context and review, checks recovery, then scans only if recovery permits it. Replacement folder/scan work waits for an occupied worker rather than starting another worker.

Closing during execution requests a safe stop and waits for the current operation/outcome handling when possible. Abrupt closure relies on journal inspection at restart; neither close nor restart grants permission to resume automatically.

## Conversation boundaries and task responsibilities

### T13: a small conversation extension

Retain the existing local-only HTTP client, strict proposal parsing, bounded transport, model checks and no-tool policy. Extend it to receive the user's organization request and bounded follow-up context separately from the filename manifest. Filenames and conversation text are untrusted data; neither can override safety policy or supply approval/root/scan identities.

The implemented T13 interface uses a strictly validated conversational response envelope with a response kind (clarification or proposal), bounded plain-text message, and the existing moves list. Clarification replies must have no moves. Proposal moves retain exactly `file_id` and `destination_folder`; pass them through the existing move parser. Reject unknown fields and malformed types. Model prose may explain or ask questions, but never supplies executable actions. T11 determines actual empty/invalid/error/result states.

Context stays in memory for the current folder/scan. The implemented bounds are 2,000 characters per user message, 4,000 per assistant message, eight user/assistant exchanges and a 16 KiB serialized conversation budget in addition to existing manifest/request bounds. T11 also rejects a completed exchange that would exceed its retained-history budget. When context is full, the app asks the user to rescan and restate all current preferences; earlier constraints are never silently dropped. There is no persistent chat storage, embedding store, external memory, model-controlled summary or autonomous loop.

Send only bounded organization text, validated prior proposal data and the existing inference manifest to local Ollama. Do not automatically include the selected absolute path, safety observations, configuration, journals or execution logs in conversation context. Do not solicit file contents or attachments. A folder change clears inference context; older messages may remain visible as inactive history. After execution, begin fresh scan context; old file IDs are not portable.

### T6: preserve the planner

T6 remains the source of reviewable operations. T11 passes the proposal's moves and trusted request/scan binding into the existing builder. Follow-ups produce a complete replacement proposal, not executable incremental commands. T6 validates the entire proposal and derives required folders before selection. Prose is never parsed into operations.

No planner redesign is required. Preserve immutable plans, identity/metadata checks, collision checks and deselection behavior. Plan IDs and observations remain local.

### T11: implemented desktop workflow

The Qt interface retains the existing controller and one background worker. T11 owns the conversation display, bounded session context, active request/revision identity, cards, subset selection, confirmation and invalidation. A `QTimer` polls queued worker completions on the GUI thread; widgets are never updated by the worker. Closing while work is active ignores the initial window-close event, requests cancellation and continues polling until the outcome is handled, without joining the worker on the GUI thread. Only one proposal is active, and repeat apply requests are rejected. The `file_assistant/ui/` package separates controller, desktop rendering, backend adapters, worker, text formatting and theme; see [UI package boundaries](./ARCHITECTURE.md#ui-package-boundaries).

Call T13 only for proposals/clarifications; T6 for plans; T9 only after explicit application-controlled confirmation. Show execution and recovery reports as application messages, clearly separate from model suggestions. Scope/refusal messages and empty/error explanations can be deterministic app text; no extra model call is needed for status reporting.

### T8/T9 and T12: retain existing responsibilities

Journaling, locking, no-overwrite execution and recovery remain unchanged by this presentation choice. The cancellation lifecycle fix in commit 99b27ee was reviewed on 2026-11-22. Known unsubmitted operations can become not_executed while retaining records; uncertain attempts still require explicit recovery acknowledgement. T11 must show the exact recovery findings and collect a separate app-controlled confirmation before calling resolve_recovery. Acknowledgement preserves historical uncertainty and permits a new reviewed plan; it never retries or resumes the old batch. Chat text cannot acknowledge recovery or clear journals.

In the implemented UI, Inspect / rescan refreshes recovery findings. Review recovery evidence opens a separate panel showing historical roots, operation paths, conclusions and observations. Only Confirm acknowledgement submits the captured evidence for backend revalidation. Changed findings, corrupt journals or unsafe/inaccessible state stay blocked. Recovery review can include journals for previously selected roots in the same trusted app-state directory; those roots are displayed explicitly. Configuration and recovery may write app-owned state before move confirmation; they do not authorize managed-file changes.

T12 still verifies the complete disposable-folder workflow and documents local setup. It should exercise conversation refinement and explicit confirmation as part of that workflow.

## Acceptance criteria and verification

- A user can request organization and refine a proposal through chat without editing JSON.
- A clarification or invalid response changes no managed files and enables no apply action.
- Move cards exactly reflect T6 output, including preserved names and necessary new folders.
- Deselecting or refining supersedes the old plan and invalidates confirmation; late responses cannot restore it.
- Typing approval-like text or receiving approval-like model text never executes operations.
- Only the app-owned confirmation for the current exact plan calls T9 once.
- No scan, chat request, clarification or review creates destination folders or moves files.
- Folder/model status stays accessible; root selection remains a trusted picker action.
- Inference and execution keep the UI responsive; cancellation/results reflect actual backend limits.
- Conversation remains bounded and local, with no content reads, tools or expanded file operations.
- Tests use fake T13 replies and disposable fixtures; one manual local-model conversation/refinement smoke test verifies the final wiring.

Current verification on 2026-11-23:

- The full suite after Developer details ran 335 tests: 333 passed and two existing real-symlink tests skipped for Windows creation permissions. UI coverage is in `tests/test_ui.py`, `tests/test_ui_qt.py` and `tests/test_ui_theme.py`; capture/retention coverage is in `tests/test_diagnostics.py`.
- Automated controller and disposable-folder service integration cover clarification/refinement, subset rebuilding, exact approval, delayed replies, cancellation with partial effects, recovery blocking/acknowledgement, file preservation and close handling. Model services/transport are faked in these tests.
- Sixteen offscreen Qt widget tests exercise actual controls, plain model/Unicode path text, Enter/Shift+Enter behavior, exact confirmation, subset rebuilding, superseded controls, GUI-thread result delivery, minimum-size confirmation visibility, recovery and close handling. They include real disposable-folder service execution with fake model transport; the GUI tests use the offscreen platform without opening desktop windows.
- Scripted native Windows Qt smoke used fake model transport and real services on disposable fixtures. It exercised clarification/refinement, visible controls, exact confirmation/cancel, execution, file preservation, separate recovery acknowledgement, clean close and the real `python -m file_assistant` module entry point. Initial and confirmation screenshots were visually inspected. A temporary wheel build verified inclusion of `theme.qss` and the runtime dependency metadata. These are not a manual live-model conversation or a full installed-release qualification.
- Developer details adds 16 capture/retention tests and 11 offscreen widget tests for success, malformed outer/assistant JSON, invalid UTF-8, timeout/incomplete-read errors, clipping, eviction, Clear during a worker request, immutable observations, distinct/stale request identities and failure isolation. Panel actions make no HTTP/disk calls and preserve approval enforcement. A separate native Qt smoke with fake HTTP connections verified raw rejected data, read-only rendering, Copy/Clear and ordinary app confirmation on disposable files; its panel screenshot was visually inspected. Clipboard access was mocked during the smoke.
- Still unverified: a live Ollama desktop conversation/refinement, broader model reliability, full keyboard/screen-reader accessibility, long-path visual stress and a DPI/window-size matrix. The T13 live-model smoke checks the backend only. The pre-migration Tkinter checkpoint passed 296 tests (two existing skips); its results do not replace Qt checks. T12 installation/deployment qualification remains separate.

## Impact on the existing plan

T11 wires the completed T13 conversation contract, T6 planning and T8/T9 execution/recovery. The user requested Qt after the UI package split and visual refresh, explicitly replacing the original Tkinter/standard-library-only UI choice. PySide6 is the added UI dependency; the controller, worker, services and backend safety boundaries are unchanged. No file-management capability was added. Review the current implementation before integration, close the remaining verification gaps, and handle broader deployment/packaging qualification under T12 when assigned. The safety contract remains authoritative.
