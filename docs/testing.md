# Windows test baseline

## Commands

Run from the project root with Python >=3.12:

```powershell
python -m unittest discover -s tests -v
python tools/qualify_windows.py
```

The first is the development suite and can report skips. The second is the strict
Windows qualification gate: Windows, NTFS temporary storage, real symlink
creation, and zero skipped/failed tests are required. It uses only disposable
temporary files. It does not change Windows security settings or grant privileges.
Exit 0 means success, 1 means test failure/skip or zero tests run, 2 means unsupported preflight.

To resolve WinError 1314, run the qualification command in an Administrator
PowerShell using the same Python interpreter, or use a Windows test environment
configured to permit symlinks. Do not elevate the future desktop app merely to
make tests work. Never mock away or remove the real-link release check.

## Recorded baseline: 2026-11-19

- Local verification was performed with Python 3.15.2 on Windows 11 build 22631, using NTFS-backed temporary storage.
- Test discovery found 15 tests: 14 passed and 1 was skipped. The skipped case was the real symlink test due to missing Windows symlink privilege (`WinError 1314`). All real junction scenarios passed.
- The previously reported junction defect is resolved; no junction regression was observed in this run.
- That restricted-session run is not a qualification pass. Separately, the developer recorded a manual privileged run in TASKS.md: Python 3.15.2, NTFS, 15 tests passed, zero failures/skips, runner exit 0. This completes the M0/T1 environment baseline; it is not product release certification.
- A release-validation run must complete with **zero skipped tests** and must record the Python version, Windows build, and filesystem used.
- Git is now initialized with a baseline commit. The origin remote points to the GitHub repository; no CI workflow is configured. The package layout is unchanged.

## Qualification runner update: 2026-11-20

The runner now rejects zero executed tests. Four unit tests cover empty, passing,
failing, and skipped suites without mocking the real filesystem qualification.
The historical manual baseline had 15 tests; the current suite has 19. Rerun the
strict command in the privileged environment to qualify this updated suite.
An unprivileged agent session may still skip the real symlink case; that does not
invalidate the separately recorded manual M0 baseline.

## Before release

- Run the full test suite on the declared minimum supported Python version (3.12) and on the pinned release runtime.
- Run validation on supported Windows 11 builds with local NTFS storage.
- Verify real filesystem behavior, including symlink and junction handling, locking/conflict scenarios, crash/restart recovery, permission failures, and disk/storage failures.
- Keep tests grouped by feature, with docstrings explaining the expected behavior.
- During development, reproduce the failure first, run focused tests while iterating, and run the full suite before handoff.
- Do not silently skip infrastructure tests. Distinguish setup/infrastructure failures from actual application regressions.
