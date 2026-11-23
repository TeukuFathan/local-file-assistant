# Conversational local file assistant UX

Updated 2026-11-23. Desktop design specification; the T13 conversation backend is implemented, but the T11 UI is not.

## Product and scope

Chat is the primary interface. Users describe how they want the selected folder organized, receive conversational replies from local Ollama, and refine suggestions through follow-up messages. Proposed moves appear as reviewable cards in the conversation. Python validates plans; only an explicit application-controlled confirmation can start execution.

The workflow is: **select folder -> describe request -> scan filenames -> discuss proposal -> review cards -> explicitly confirm -> see actual results**.

This user-approved UX replaces the form-oriented T11 presentation in TASKS.md and ARCHITECTURE.md. Their safety boundaries and backend responsibilities still apply. The conversation-input/reply extension described below is implemented in T13 (`01e3753`), preserving the original moves-only API. The desktop chat, review cards and UI approval lifecycle remain T11 work. TASKS.md records the backend verification and next implementation step.

The MVP still organizes only regular files directly inside one user-selected folder, using filenames and extensions. Preserve basenames and move only into one-level child folders under that root. No file-content reads, previews, attachments, recursive organization, deletion, arbitrary rename, file launching, shell commands, cloud inference, autonomous tools, or general-purpose agent behavior.

## Screen and interaction

The main area is a scrollable conversation with a text composer and Send button. Enter sends a message; it never confirms file operations. Messages, proposal cards, confirmation controls and execution reports are visually distinguishable.

A compact persistent header shows the selected folder, a Change folder control, the selected local model and Ollama availability. These are secondary controls, but the active folder must remain visible during review and confirmation. The model cannot select or change it. Selecting a folder does not itself send filenames to Ollama; a submitted organization request starts the scan/proposal flow. A request entered before selection remains local until a valid folder is chosen.

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

## Review cards and confirmation

Each proposal is a versioned card group rendered from the T6 plan, not from model prose or model-supplied paths. Each move card shows:

- Exact source and destination relative paths, preserving the filename.
- A selected/deselected control.
- Whether its destination folder already exists or will be created.

Show a batch summary with selected move count and all required new folders. Long names must be inspectable in full without opening the file; do not rely on a truncated label for review. Render filenames and model text as inert text, never executable markup or file links. Do not expose IDs or filesystem identity metadata as normal user controls.

Deselecting a move rebuilds the T6 subset plan and removes unnecessary creations. Follow-up messages can change grouping or destination folder names within the existing scope. Every revision produces a fresh plan identity and invalidates any earlier confirmation. Old cards remain visible as Superseded, with their action controls disabled. Only one proposal is active.

Review and apply opens an application-controlled confirmation panel showing the current folder, exact selected moves and required folder creations. Confirm moves explicitly approves that immutable plan once and hands it to T9. Cancel returns to review without execution. Disable apply for empty, invalid, stale, superseded, already submitted or recovery-blocked plans. Immediately disable repeat submissions after confirmation.

Typed "yes", "apply", "go ahead", model approval fields, and assistant prose never mint approval. Direct such messages to the review controls when appropriate. The app's confirmation controls cannot be generated by model text. Changing the selected folder, rescanning, revising the proposal or changing the selection closes any pending confirmation and invalidates its approval.

## Minimum conversation states

These are UI states, not a new workflow framework. Ollama availability is a separate status indicator, so an offline model does not hide existing results or recovery information.

| State | Conversation behavior | Backend connection and permitted transition |
| --- | --- | --- |
| Awaiting folder | Explain scope; retain an unsent request locally; show Choose folder. | Trusted folder picker/configuration and validators. No proposal or execution. Valid selection enters Ready. |
| Ready | Accept an organization request; show active folder and model. | T11 dispatches T5 scanning on submission. |
| Preparing | Show Scanning or Asking local model; allow cancellation; prevent overlapping submissions. | T5 must finish completely before T13. Capture root, scan ID and request/revision identity locally. Valid reply enters Clarifying or Reviewing; error enters Blocked. |
| Clarifying | Display a short model question and accept a follow-up. No apply control. | Bounded T13 conversation context. A subsequent reply may produce a fresh proposal. |
| Reviewing | Display model explanation plus Python-validated move cards; accept refinement/deselection. Confirmation panel is a substate here. | T13 proposals pass T6 before cards become actionable. Refinement returns to Preparing; explicit app confirmation starts T9 execution. |
| Executing | Show application progress and Stop; disable changes to folder, proposal, selection and approval. | T9 uses T8 journaling/locking. Cancellation stops at supported operation boundaries; never promise rollback. |
| Finished | Show completed, failed/uncertain and untouched operations, including created folders. | Render T9 results, not model claims. Before another organization proposal, rescan and establish fresh IDs/context. |
| Blocked | Explain invalid selection, failed/incomplete scan, stale plan, unavailable model, malformed response or recovery requirement. | Retry only through the relevant explicit user action. Recovery blockers disable execution until safely resolved; restarting chat does not clear journals. |

Cancelling inference invalidates its request identity immediately; discard late results even if the HTTP call cannot yet be interrupted. Keep the single worker busy until it finishes rather than starting another inference concurrently. Changing folder or starting a new scan invalidates all earlier in-flight responses and actionable cards. A late result must not replace a newer review or reuse recurring file IDs from another scan.

Closing during execution requests a safe stop and waits for the current operation/outcome handling when possible. Abrupt closure relies on journal inspection at restart; neither close nor restart grants permission to resume automatically.

## Conversation boundaries and task responsibilities

### T13: a small conversation extension

Retain the existing local-only HTTP client, strict proposal parsing, bounded transport, model checks and no-tool policy. Extend it to receive the user's organization request and bounded follow-up context separately from the filename manifest. Filenames and conversation text are untrusted data; neither can override safety policy or supply approval/root/scan identities.

The implemented T13 interface uses a strictly validated conversational response envelope with a response kind (clarification or proposal), bounded plain-text message, and the existing moves list. Clarification replies must have no moves. Proposal moves retain exactly `file_id` and `destination_folder`; pass them through the existing move parser. Reject unknown fields and malformed types. Model prose may explain or ask questions, but never supplies executable actions. T11 determines actual empty/invalid/error/result states.

Keep context in memory for the current folder/scan. A practical initial bound is 2,000 characters per user message and eight user/assistant exchanges, with a 16 KiB total conversation budget in addition to existing manifest/request bounds. Reject oversized input or ask the user to restate current preferences when context is full; do not silently drop earlier constraints. Do not add persistent chat storage, embeddings, external memory, model-controlled summaries or an autonomous loop.

Send only bounded organization text, validated prior proposal data and the existing inference manifest to local Ollama. Do not automatically include the selected absolute path, safety observations, configuration, journals or execution logs in conversation context. Do not solicit file contents or attachments. A folder change clears inference context; older messages may remain visible as inactive history. After execution, begin fresh scan context; old file IDs are not portable.

### T6: preserve the planner

T6 remains the source of reviewable operations. T11 passes the proposal's moves and trusted request/scan binding into the existing builder. Follow-ups produce a complete replacement proposal, not executable incremental commands. T6 validates the entire proposal and derives required folders before selection. Prose is never parsed into operations.

No planner redesign is required. Preserve immutable plans, identity/metadata checks, collision checks and deselection behavior. Plan IDs and observations remain local.

### T11: implement the chat-first desktop workflow

Use the planned Tkinter interface and one background worker. T11 owns the conversation display, bounded session context, active request/revision identity, cards, subset selection, confirmation and invalidation. Widgets update on the UI thread. Maintain a single active proposal and prevent duplicate apply requests.

Call T13 only for proposals/clarifications; T6 for plans; T9 only after explicit application-controlled confirmation. Show execution and recovery reports as application messages, clearly separate from model suggestions. Scope/refusal messages and empty/error explanations can be deterministic app text; no extra model call is needed for status reporting.

### T8/T9 and T12: retain existing responsibilities

Journaling, locking, no-overwrite execution and recovery remain unchanged by this presentation choice. The cancellation lifecycle fix in commit 99b27ee was reviewed on 2026-11-22. Known unsubmitted operations can become not_executed while retaining records; uncertain attempts still require explicit recovery acknowledgement. T11 must show the exact recovery findings and collect a separate app-controlled confirmation before calling resolve_recovery. Acknowledgement preserves historical uncertainty and permits a new reviewed plan; it never retries or resumes the old batch. Chat text cannot acknowledge recovery or clear journals.

T12 still verifies the complete disposable-folder workflow and documents local setup. It should exercise conversation refinement and explicit confirmation as part of that workflow.

## Acceptance criteria for implementation

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

## Impact on the existing plan

This is a meaningful change to T11 and a contained extension to T13, not a replacement of the safety architecture. Keep T2/T3 path validation, T5 scanning, T6 planning and T8/T9 execution/recovery. Implement the T13 conversational contract and tests before wiring T11 to it. Wire and test the reviewed T9 cancellation/journal-completion behavior in T11 before claiming the MVP execution experience is complete. No new platform, agent framework or file-management capability is required.
