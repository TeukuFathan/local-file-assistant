# Conversational local file assistant UX

Updated 2026-11-23. Desktop UX specification and current T11 implementation notes. The conversational UI and visual refresh are implemented in the `feat/T11-chat` working tree, pending review and integration. Deployment qualification remains T12 work.

## Product and scope

Chat is the primary interface. Users describe how they want the selected folder organized, receive conversational replies from local Ollama, and refine suggestions through follow-up messages. Proposed moves appear as reviewable cards in the conversation. Python validates plans; only an explicit application-controlled confirmation can start execution.

The workflow is: **select folder -> describe request -> scan filenames -> discuss proposal -> review cards -> explicitly confirm -> see actual results**.

This user-approved UX replaces the earlier form-oriented T11 presentation. The safety boundaries and backend responsibilities in TASKS.md and ARCHITECTURE.md still apply. The T13 conversation extension (`01e3753`) preserves the original moves-only API. T11 now connects it to desktop chat, review cards, explicit confirmation and execution/recovery reporting. TASKS.md records verification and remaining work.

The MVP still organizes only regular files directly inside one user-selected folder, using filenames and extensions. Preserve basenames and move only into one-level child folders under that root. No file-content reads, previews, attachments, recursive organization, deletion, arbitrary rename, file launching, shell commands, cloud inference, autonomous tools, or general-purpose agent behavior.

## Screen and interaction

The main area is a scrollable conversation with a bottom text composer and Send button. Enter in the composer sends a message; it never confirms file operations. Messages, proposal cards, confirmation controls and execution reports are visually distinguishable.

A persistent context panel shows the selected folder and Change folder control, a Local model text field with Select model, Inspect / rescan, and Stop. The user enters the exact name of an already installed local Ollama model and explicitly selects it. There is no automatic model selection or download. The status line shows conversation state, the selected model, and the latest request status; it is not a continuously polled Ollama health indicator.

These are secondary controls, but the active folder remains visible during review and confirmation. The model cannot select or change it. Selecting a folder validates and saves the choice and inspects recovery records; it does not send filenames to Ollama. A submitted request scans filenames when a fresh scan is needed. Follow-ups reuse the current complete scan. An unsent request remains local in the composer; selecting a folder does not submit it automatically. Each app launch starts without an active folder or model, even though folder selection is saved in app configuration.

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
| Shape and spacing | Rounded panels up to 18 Tk pixels, button image corners of 11 pixels, typically 18 pixels of panel padding and 28 pixels of outer horizontal spacing. |
| Typography | Title 23 points bold, body/composer 11–12 points, action labels 10 points bold, secondary text 9–10 points. |
| Fonts | Prefer installed Inter, then Inter Variable, Segoe UI Variable Text, Segoe UI, Arial, then the existing system default. Fonts are not bundled, installed or downloaded. The current Windows smoke used Segoe UI Variable Text because Inter was absent. |

Rounded surfaces use Tk canvas backdrops containing ordinary widgets. Action buttons remain native `ttk.Button` controls with rounded image styling, focus and disabled-state treatment. No additional UI framework, downloaded artwork or new dependency is used. Theme definitions live in `file_assistant/ui/theme.py`; widget construction and rendering live in `desktop.py`.

Messages and exact paths are selectable, copyable, inert text. They wrap within their panel, and long text has a vertical scrollbar; a shortened label is never the only way to inspect a move. The conversation scrolls to the newest content after layout so confirmation controls are reachable. The window currently opens at 1080 × 850 with an 820 × 650 minimum; broad small-window, DPI and accessibility qualification is still pending.

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
| Awaiting folder (`no_folder`) | Explain scope; retain an unsent request locally; show Change folder. | Trusted folder picker/configuration and validators. No proposal or execution. Valid selection with no recovery blocker enters Ready. |
| Ready (`ready`) | Accept an organization request; show active folder and model. | T11 uses the complete current scan or dispatches T5 scanning on submission. An explicitly selected model is required for inference. |
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

The Tkinter interface uses one background worker. T11 owns the conversation display, bounded session context, active request/revision identity, cards, subset selection, confirmation and invalidation. Widgets update on the UI thread. Only one proposal is active, and repeat apply requests are rejected. The user-requested split into the `file_assistant/ui/` package separates controller, desktop rendering, backend adapters, worker, text formatting and theme; see [UI package boundaries](./ARCHITECTURE.md#ui-package-boundaries).

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

- The full automated suite ran 296 tests: 294 passed and two existing real-symlink tests skipped for Windows creation permissions. UI/theme coverage is in `tests/test_ui.py` and `tests/test_ui_theme.py`.
- Automated controller and disposable-folder service integration cover clarification/refinement, subset rebuilding, exact approval, delayed replies, cancellation with partial effects, recovery blocking/acknowledgement, file preservation and close handling. Model services/transport are faked in these tests.
- Scripted native Tk smoke used fake model transport and real Windows services on disposable fixtures. It exercised visible controls, proposal and exact confirmation, cancelling confirmation, execution, file preservation, separate recovery acknowledgement and clean close. Initial and confirmation screenshots were visually inspected. This is not a manual live-model conversation.
- Still unverified: a live Ollama desktop conversation/refinement, broader model reliability, full keyboard/screen-reader accessibility, long-path visual stress and a DPI/window-size matrix. The previously recorded T13 live-model smoke checks the backend only. T12 installation/deployment qualification remains separate.

## Impact on the existing plan

T11 now wires the completed T13 conversation contract, T6 planning and T8/T9 execution/recovery. The UI package split and visual refresh preserve those boundaries and introduce no new file-management capability or dependency. Review the current implementation before integration, close the remaining verification gaps, and handle local deployment documentation and packaging under T12 when assigned. The safety contract remains authoritative.
