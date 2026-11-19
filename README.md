# Local File Assistant

A Windows desktop file-management project in development. The current code is
an unfinished path validator, not a usable file manager. Do not use it to
authorize operations on real user files.

## Runtime and current commands

Python 3.12 or newer is required. Initial product target: Windows 11, local fixed
NTFS storage, one explicitly selected existing workspace. Release support is
not yet qualified. No runtime dependencies are required for the current tests.
Setuptools is a build-only dependency for standard Python package metadata and
future installation; it is not required to run from this checkout.

From the repository root:

```powershell
python --version
python -m unittest discover -s tests -v
python tools/qualify_windows.py
```

The last command rejects skipped tests and requires a Windows NTFS environment
where real symlinks can be created. See [testing instructions](./docs/testing.md).
`python -m file_assistant` currently exits without output: no CLI or desktop
commands have been implemented. The sample sandbox constant does not create or
configure a folder automatically.

## Agreed scope

Never launch user files, read their contents, overwrite existing destinations,
or permanently delete them. Future deletion means recoverable Trash. Mutations
require safety validation, appropriate confirmation, and recovery records.
These are requirements, not completed features.

- [Safety contract](./SAFETY_CONTRACT.md): authoritative behavior rules.
- [Architecture](./ARCHITECTURE.md): decisions, module boundaries, and limitations.
- [Tasks](./TASKS.md): dependencies, ownership, acceptance criteria, and status.
- [Original guide](./Windows_Local_File_Assistant_Guide.docx): historical only.
  Its single-script commands, Python 3.11 minimum, opening files, grouping,
  external trash, and deferred tests/recovery conflict with the current contract.

Development follows tests first, using temporary fixtures. No AI integration
until the core product is complete; no unrestricted shell tools in future AI.
