# Incremental development plan

Status: proposed backlog, 2026-11-19. No feature implementation is authorized by this file. See [ARCHITECTURE.md](./ARCHITECTURE.md) and the existing [safety contract](./SAFETY_CONTRACT.md).

## Working rules

- Preserve current modules/tests. Do not move code into a new layout or rewrite working checks.
- Developer implements unless a specific task is delegated. Tests precede behavior changes, with descriptive docstrings and function/feature groups.
- One agent owns each writable file at a time. Own tests with their corresponding implementation; independent review is read-only.
- Each task reports changed files, commands/results, expected failures versus regressions, skips, and limitations. No tests against the real configured sandbox or user files.
- Planned paths below are future files, not instructions to create placeholders now. Existing Python files remain untouched by this planning task.

## Milestones and tasks

### M0: approve the product boundary and establish the baseline

**T0 - Contract reconciliation and interface decisions**

- Status: COMPLETE (2026-11-19). User authorized completion; decisions are recorded
  in the safety contract, architecture M0 interface agreement, and README.

- Owner: developer/architecture lead.
- Files: `ARCHITECTURE.md`, `TASKS.md`, `SAFETY_CONTRACT.md`, `README.md` (new). Do not edit AGENTS or the original guide without an explicit reason/approval.
- Dependencies: none.
- Acceptance: approve or revise the proposal's OS/filesystem scope, root/empty-path semantics, Python minimum, reserved-state ownership, hard-link policy, missing-parent restore behavior, and crash/race limitations. README labels the guide historical and identifies current commands accurately. No unsupported claim of complete safety. Record approved request/result/plan signatures before parallel writers start.

**T1 - Windows test baseline and supported runtime**

- Status: COMPLETE (2026-11-19). Done manually.
  Windows environment qualification passed.
  Python 3.15.2; NTFS confirmed.
  15 tests passed, 0 failures, 0 skipped.
  Real symlink and junction tests passed.
  Qualification runner exited with code 0.
  GitHub CI remains optional and is not configured.

- Owner: test/release agent.
- Files: `pyproject.toml`, `tools/qualify_windows.py`, `docs/testing.md`; `.github/workflows/tests.yml` only when a CI task is explicitly assigned; GitHub hosting is configured but CI is not.
- Dependencies: T0 runtime/support decisions.
- Acceptance: metadata states Python >=3.12; the existing package imports without relocation. Run current unittest discovery on the chosen Windows baseline and record the existing junction failure separately from infrastructure failures. Establish a Windows environment that can run real link tests; no blanket skip to make CI green. No release gate waived because a test is mocked. New build dependencies explained before introduction.

### M1: complete the filesystem safety boundary

**T2 - Finish existing-path and root validation**

- Owner: path-policy agent.
- Files: `file_assistant/workspace.py`, `tests/test_workspace.py`.
- Dependencies: T0; integrate with T1 baseline.
- Increment: junction rejection at the requested folder, ancestor folder, and workspace root is complete as of 2026-11-19. Continue with root validation, names, containment, and successful returns, one rule at a time.
- Acceptance: absolute existing directory root is required; root ancestors and requested components cannot redirect through links/junctions/unsupported reparse points. Reject invalid Windows/device/ADS/trailing-dot-space inputs and reserved-state aliases. Test `.` versus empty input, sibling-prefix containment, nonexistent paths, permissions, case, Unicode, long paths, and applicable short-name behavior. Return the canonical validated Path for supported existing objects. Preserve old tests, strengthen the accepted-path test, and distinguish missing versus inaccessible paths. Document unsupported filesystem cases and remaining concurrent-substitution limitations.

**T3 - Destination validation and mutation identity contract**

- Owner: same path-policy agent (serialize with T2).
- Files: `file_assistant/workspace.py`, `tests/test_workspace.py`.
- Dependencies: T2 and T0 mutation policy.
- Acceptance: separate destination validator supports an absent basename under an existing safe parent; refuses collisions, special names, reserved roots/state, and invalid parents. Tests cover absent and occupied destinations, invalid names, unsafe parents, and root validation at call time. Define the identity and metadata requirements for later mutation revalidation. Preview/execution scenarios, case-only rename refusal, and mutation hard-link enforcement belong to T9; do not build an operation layer in T3. No API promises race freedom merely by returning a Path.

### M2: read-only core and desktop preview

**T4 - Shared models and configuration**

- Owner: application-contract agent.
- Files: `file_assistant/models.py`, `file_assistant/config.py`, `tests/test_models.py`, `tests/test_config.py` (new).
- Dependencies: T0; configuration validation integrates T2.
- Acceptance: minimal entries/request/plan/result structures; no generic framework. Root selection is explicit and persists atomically in app-local state. Missing/corrupt config does not widen scope. Switching roots invalidates plans. Tests use injected temporary state paths. No filesystem mutation is hidden inside models. Freeze signatures before T5/T6/T8 integration.

**T5 - List and filename search**

- Owner: operations agent.
- Files: `file_assistant/operations.py`, `tests/test_operations.py`.
- Dependencies: T2, T4.
- Acceptance: immediate-child listing and recursive filename-fragment search return relative entries in deterministic order with documented case matching. Do not read file contents or follow redirects; reserve Trash. Include root listing, nested/empty folders, Unicode, invalid input, inaccessible/disappearing entries, cancellation, and large-tree fixtures. Report partial results explicitly. No printing, prompts, UI imports, or writes to the workspace.

**T6 - Desktop shell using fixtures**

- Owner: desktop agent.
- Files: `file_assistant/ui.py`, `tests/test_ui.py` (new).
- Dependencies: T0 desktop decision, T4 interface freeze; can proceed before T5 using fixtures.
- Acceptance: selected-root display, list/search views, empty/error/progress states, keyboard navigation, readable scaling, and cancellation. One worker/message queue keeps UI responsive; widgets updated only on UI thread. Tests use fake services and no real user files. No mutation buttons enabled yet. Prototype records accessibility/performance issues before choosing a larger toolkit.

**T7 - Integrate read-only desktop and qualify milestone**

- Owner: integration lead.
- Files: `file_assistant/__main__.py`, `tests/test_app.py` (new), `README.md`.
- Dependencies: T1, T5, T6.
- Acceptance: `python -m file_assistant` opens the read-only app with correct composition. GUI listing/search use the real validated operations. Startup/config failures are understandable, close/cancel are responsive, and a disposable-workspace smoke test proves no mutation or launch. Existing tests pass; all safety skips are documented. Other owners fix their modules through separate handoffs.

### M3: mutation foundations before enabling actions

**T8 - Durable recovery store, writer lock, and logging**

- Owner: recovery agent.
- Files: `file_assistant/recovery.py`, `tests/test_recovery.py`, `docs/recovery.md` (new).
- Dependencies: T0 storage decisions, T4.
- Acceptance: exclusive per-workspace app writer lock, durable intent/outcome records, unique IDs and original relative locations, schema/version handling, and ownership-checked state initialization. Cover disk-full, permission, corrupt state, concurrent instances, and restart with incomplete records. Define source-only/destination-only/both/neither reconciliation; ambiguity blocks conflicting actions and never deletes data. Distinguish activity-log rotation from permanent recovery retention. Can test against fake filesystem actions before T9/T10.

**T9 - Folder creation then move/rename**

- Owner: operations agent.
- Files: `file_assistant/operations.py`, `tests/test_operations.py`.
- Dependencies: T3, T4, T8; after T5 relinquishes these files.
- Acceptance: test-first separate increments for a single child-folder creation, then single-file move/rename. Folder creation is immediate but validated/logged. Moves require immutable preview plus explicit authorization; stale/single-use plans are rejected. Use verified same-volume no-overwrite execution, never copy/delete fallback. Test destination appearance after preview and execution-time destination races, root changes after preview, case-only rename refusal, multiply-hard-linked source refusal, locked files, source replacement, permission failures, concurrent instances, and journal failure before/after the move. Success only after outcome persistence; uncertainty is reported accurately.

**T10 - Trash and restore**

- Owner: operations agent; recovery agent supplies review rather than concurrent edits.
- Files: `file_assistant/operations.py`, `tests/test_operations.py`.
- Dependencies: T9 and T8 recovery protocol.
- Acceptance: preview/confirm one file at a time; unique Trash payload and durable original-path record; safe dedicated Trash listing. Restore rejects conflicts and missing original parent. No purge, permanent deletion, automatic suffixes, or launch. Corrupt records leave payloads intact. Inject failures at every journal/move boundary, restart and verify reconciliation; real subprocess interruption tests use disposable fixtures only. Never reset an existing ambiguous Trash directory.

**T11 - Desktop mutation flows**

- Owner: desktop agent.
- Files: `file_assistant/ui.py`, `tests/test_ui.py`.
- Dependencies: T9 then T10; fixture work may begin once plans/results freeze.
- Acceptance: exact source/destination preview, explicit confirmation, safe default cancellation, visible failures/recovery state, and dedicated Trash restore view. No double execution from repeated clicks; disable mutation while an operation or reconciliation is pending. UI cannot bypass service validation. Cancellation/shutdown explains whether an operation already happened and preserves recovery state.

### M4: release qualification

**T12 - Distribution and failure qualification**

- Owner: release agent; integration lead approves.
- Files: `packaging/` (new), build configuration in `pyproject.toml`, CI workflow, `docs/testing.md`, `docs/release-checklist.md` (new). README edits belong to the lead.
- Dependencies: T1, T7, T10, T11; packaging research can start earlier.
- Acceptance: choose bundler/installer only after dependency tradeoffs are reviewed. Clean-machine launch without developer Python, standard-user operation without elevation, tested supported Windows/NTFS matrix, accessibility/scaling checks, and large-tree responsiveness. Real link/junction/lock/crash tests run rather than skip in release qualification. Installer/upgrades/uninstall never erase workspaces, Trash, or recovery records. Validate preserved state across upgrades; document signing/distribution decisions and known limits. No external telemetry or network dependency for file operations.

### Deferred: requires a separate scope decision

Bulk grouping, copy, recursive folder mutations, network/cloud/removable roots, content indexing, background monitoring, general undo, and AI. AI begins only after the core product is qualified, through the same typed operation boundary and user-controlled authorization. Permanent deletion and launching user files remain prohibited unless the safety contract is deliberately changed.

## Parallel agent allocation and handoffs

The user has requested identification of parallel tasks, not execution of them. Do not launch implementation agents until tasks are assigned.

| Wave | Independent work | Required handoff |
| --- | --- | --- |
| M1 now | One developer handles T2 then T3; reviewer follows each completed slice | M0 is complete. No parallel writers in workspace.py or its tests. |
| After T2/T4 | T5 read-only operations and T6 fixture desktop | Two independent owners use frozen models; defer T8 until mutation planning. |
| After T5 | UI refinement and packaging research if useful | T3 belongs to M1; do not treat it as complete if deferred. T9 waits for T3/T8. |
| Mutation wave | T9 then T10 by one operations owner; T11 fixture UI by desktop owner | Complete T8 first. Queue release work rather than start a third subagent. |

Parallel work is not permitted in the same file even on separate task branches without explicit coordination. Shared model changes go through T4's owner; shared docs/entry-point changes go through the integration lead. Each handoff lists API changes, test evidence, skipped checks, and unresolved assumptions. Reviewers may inspect other files but must not edit them. Keep one integrating lead and at most two active subagents, matching .codex/config.toml. For M1, use one developer followed by one read-only reviewer; use the researcher only for a specific unresolved Windows question.

## Immediate next action

M0 (T0 and T1) is complete, including the developer-recorded manual qualification run. M1 is not complete. Continue T2 with absolute/existing-directory root validation and workspace-ancestor checks when authorized. Do not start mutation implementation while the safety boundary remains unfinished. The first usable product checkpoint is the read-only desktop at T7, not the full backlog at once. The 2026-11-20 runner update adds four tests; rerun strict qualification for that updated suite in the privileged environment.

## Resume checkpoint and economical delegation

Checkpoint 2026-11-19: the junction slice is finished. Tests were expanded first and failed at the requested-folder and ancestor-folder cases; the component-loop guard was then added. Full command: `python -m unittest discover -s tests -v`. Result: 15 methods run, 14 pass, 1 real-symlink test skips because Windows denies symlink creation. All fixtures were disposable. No user-file operations or desktop/mutation features were added.

The existing safety contract, package layout, commented tests, and architecture proposal are in place. T2 now returns canonical paths and implements root/ancestor validation, Windows-name rules, and containment checks. T4 configuration now reuses that validator and rejects overlap with application state. Request parsing rejects rooted paths and malformed required/forbidden fields. This is not authorization to expose configuration or arbitrary workspace roots to AI tools; trusted application code must choose the root. Current integration tests do not certify future mutation operations.

### Next assignments, in order

1. Use the recorded T0 decisions; do not ask each agent to redesign the project.
2. Assign one implementation agent a bounded T2 slice: root validity and ancestors, then a separate slice for Windows names, then containment/return behavior. Exclusive ownership: `workspace.py` and `test_workspace.py`. Tests first. Keep the same agent for these related slices to reuse context. Do not split tests and implementation across competing writers.
3. T1 is complete. Delegate infrastructure work only for a specific new gap; do not repeat baseline setup or modify path tests to hide permission-related skips.
4. After the validator and interfaces stabilize, delegate operations T5 and fixture UI T6 independently; assign recovery T8 only when mutation planning begins. Do not preload future agents with the whole backlog or keep them waiting on dependencies.
5. The lead integrates once per completed slice. Use an independent read-only review for the completed safety boundary and later recovery protocol, not for every two-line edit.

### Delegation entry point

The lead assigns a specific TASKS.md task or slice, its acceptance criteria, exact writable files, and relevant ARCHITECTURE.md section. Business-role labels such as path-policy agent map to the configured developer role, not additional agent definitions. Developer reads the safety contract and those assigned sections before editing. Reviewer receives the same brief plus the resulting diff and test evidence. No role should independently redesign the backlog or launch nested agents. Read repository instructions only where applicable; current explicit user instructions take precedence.

### Short task brief template

```text
Task: <one task ID and bounded acceptance criteria>
Read: AGENTS.md, SAFETY_CONTRACT.md, the relevant TASKS section,
      the relevant ARCHITECTURE section, and owned source/tests.
Own/write: <exact file list>. Other files are read-only.
Baseline: <latest test command, result, known failures/skips>.
Do: tests first, minimal changes, temporary fixtures only.
Do not: redesign architecture, add dependencies, implement adjacent tasks,
        launch more agents, or operate on real user files.
Return: changed files; acceptance criteria met; test results/skips;
        remaining risks or decisions. Keep the handoff concise.
```

Use a fresh narrowly scoped agent when context is unrelated; reuse an existing agent for follow-up in its owned module. Send this brief and file references instead of full chat history. Agents should inspect referenced files once and reread only changed sections. Request concise results, not full logs unless a failure needs diagnosis. Run focused checks while iterating and the full suite at integration. Never save tokens by skipping safety acceptance criteria. No additional agents were started for this checkpoint.
