# Project Instructions

## Goal
Build a reliable, lightweight Windows file-management application using Python.

## Engineering principles
- Prioritize correctness, safety, simplicity, and maintainability.
- Prefer Python's standard library.
- Keep dependencies minimal.
- Separate business logic from the user interface.
- Do not introduce AI functionality until the core application is complete.
- Avoid unnecessary abstractions, frameworks, and features.

## File safety
- Restrict operations to explicitly configured directories.
- Never permanently delete files by default.
- Use the Recycle Bin or a recoverable quarantine.
- Preview destructive or bulk operations before execution.
- Handle name conflicts without overwriting existing files.
- Validate paths, including resolved paths and symlinks.
- Avoid partially completed bulk operations when possible.

## Development workflow
1. Inspect existing code before making changes.
2. For significant changes, explain the plan first.
3. Implement one feature at a time.
4. Add automated tests for new behavior.
5. Run relevant tests after changes.
6. Fix regressions before considering a task complete.
7. Summarize changed files and test results.

## Restrictions
- Do not rewrite working modules unnecessarily.
- Do not add dependencies without explaining why.
- Do not make unrelated changes.
- Never run destructive operations against real user files during tests.
- Ask before changing the project's architecture.

## Definition of done
A feature is complete only when its expected behavior, error cases, and safety checks have been tested.