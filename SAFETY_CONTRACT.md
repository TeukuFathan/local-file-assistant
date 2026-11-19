# Safety contract — first version

Agreed during Milestone 1. These are requirements for future implementation,
not a claim that the application or its safeguards already exist.

## 1. Workspace boundary

- Manage files only inside one explicitly configured local sandbox.
- The planned test location is `C:\AssistantSandbox`; use disposable sample files only.
- Accept user paths relative to the sandbox. Refuse absolute paths and attempts to escape it.
- Validate resolved paths as well as the supplied paths. Refuse symbolic links and Windows junctions, including in parent paths.
- Never move, rename, or trash the sandbox root.
- Folder names such as `Private` and `Users` do not grant special protection.

## 2. Supported operations

- List files and folders, and search filenames without reading user-file contents.
- Create folders.
- Move or rename one regular file at a time.
- Send one regular file to Trash, or restore one trashed file.
- Never open files in another application, launch them, or execute them.
- No moving or trashing whole folders, bulk grouping, or AI integration in this initial scope.

## 3. Preview and confirmation

- Listing, searching, and folder creation run without a confirmation prompt.
- Moving, renaming, trashing, and restoring show the proposed change and require explicit confirmation.
- Cancellation or an empty answer changes nothing.
- Revalidate before execution. If relevant source or destination conditions have changed since preview, cancel and require a fresh preview.

## 4. Trash and restoration

- Reserve `Trash` inside the sandbox for application-managed quarantine; it is not the Windows Recycle Bin.
- Delete means moving a file to Trash, never permanently erasing it.
- Give each trashed item a unique identifier and retain its original relative location for restoration.
- Exclude Trash from normal search and file-management operations. Access it through dedicated trash listing and restore operations.
- Restore to the original location only when safe; never overwrite an occupied destination.
- If recovery information cannot be saved, refuse the trash operation.
- If moving to Trash fails, report failure; never fall back to permanent deletion.
- Provide no permanent-delete command, automatic cleanup, or empty-trash feature.
- Trashed files continue to occupy disk space. This policy controls the app, not manual actions in File Explorer.

## 5. Conflicts and failures

- Never overwrite an existing destination or silently choose a different name.
- If a file is missing, locked against the requested operation, or inaccessible, stop and explain the failure.
- Execute one file operation at a time in the initial version.
- After interruption, verify what happened and report uncertainty honestly. Never claim success without checking.

## 6. Basic records

- Introduce basic logging with the first file-changing features.
- Record time, action, original and destination paths where applicable, and success or failure with its reason.
- Never record user-file contents.
- Maintain reliable trash recovery records; a diagnostic log alone is not enough to restore files.
- A log viewer can be added later.

## 7. Verification during implementation

Use temporary directories and disposable copies, never real user files.
For each feature, test expected behavior, errors, and the applicable rules above.
In particular, verify:

- Paths cannot escape the configured root, including through links or junctions.
- Existing destination files remain unchanged after conflicts.
- Preview and cancellation make no changes.
- Changed conditions invalidate a preview.
- Trash and restore preserve files and their recovery information.
- Failed or interrupted operations are reported accurately.
- Normal operations cannot manipulate Trash or the sandbox root.
- No command launches user files or permanently deletes them.

## M0 decisions (2026-11-19)

These clarify the product requirements; incomplete code does not yet enforce them.

- Target Windows 11 on local fixed NTFS storage, Python >=3.12. Network shares,
  cloud-managed roots, removable media, and other filesystems are not supported
  in the first release. Exact release build qualification remains a release task.
- The configured root must be absolute and already exist. `.` permits root
  listing/search; empty user paths are rejected. Root mutation is always refused.
- Initially refuse mutations of multiply-hard-linked files and case-only renames.
  Do not silently fall back to copying/deleting across volumes.
- Folder creation creates one child beneath an existing validated parent.
- Trash initialization must explicitly establish app ownership. Never adopt or
  replace an existing unrelated Trash folder. Store payloads and recovery state
  there; app-local configuration/activity logs live separately under LocalAppData.
  App-owned state cannot be a managed workspace or ordinary operation target.
- Restore refuses a missing original parent as well as an occupied destination.
- This utility is not an OS security sandbox. Revalidation cannot guarantee
  safety against hostile concurrent path substitution. Do not claim otherwise.
- Recovery preserves uncertain data and blocks conflicting changes; no automatic
  deletion to resolve ambiguity. Hardware-loss recovery is not guaranteed.

## Next milestone

Milestone 2: implement and test path validation, listing, and filename search.
Implementation remains the primary developer's responsibility unless explicitly delegated.
