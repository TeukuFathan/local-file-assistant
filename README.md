# Local File Assistant

A Windows desktop filename-only organizer in development, with a PySide6 (Qt)
conversation UI and local Ollama proposals. Python validates moves and requires
explicit review and confirmation. Use disposable fixtures while deployment
qualification remains incomplete.

## Runtime and current commands

Python 3.12 or newer is required. Initial product target: Windows 11, local fixed
NTFS storage, one explicitly selected existing workspace. Release support is
not yet qualified. The UI and its tests require `PySide6-Essentials>=6.12,<7`,
which provides the Qt Widgets modules without unused add-on packages.
Setuptools is a build-only dependency for standard Python package metadata and
installation; it is not a runtime dependency.

From the repository root:

```powershell
python --version
python -m pip install -e .
python -m unittest discover -s tests -v
python tools/qualify_windows.py
python -m file_assistant
```

The last command rejects skipped tests and requires a Windows NTFS environment
where real symlinks can be created. See [testing instructions](./docs/testing.md).
`python -m file_assistant` opens the Qt desktop with `qwen3.5:9b` active by
default; no model-selection step is required. Click the light-blue Choose folder
button, choose a disposable folder, then send an organization request. Change
model is an optional override. Ollama must be running for proposals; the
app does not download models. Review and apply opens the exact summary; only
Confirm moves authorizes file changes. The app starts without an active folder.
The sample sandbox constant does not configure a folder automatically.

Developer details opens a read-only view of the latest four conversation
requests: exact sent JSON, captured raw/invalid replies, validated proposals and
errors. Data is bounded and kept in memory only. Copy is manual clipboard access;
Clear changes no chat, approval or files. See [UX details](./UX.md#developer-details)
for capture limits. The panel never runs JSON or authorizes moves.

## Agreed scope

Never launch user files, read their contents, overwrite existing destinations,
or permanently delete them. Deletion is outside the current UI scope. Mutations
require safety validation, appropriate confirmation, and recovery records.
See the safety contract and UX verification limits before broader use.

- [Safety contract](./SAFETY_CONTRACT.md): authoritative behavior rules.
- [Architecture](./ARCHITECTURE.md): decisions, module boundaries, and limitations.
- [Tasks](./TASKS.md): dependencies, ownership, acceptance criteria, and status.
- [Original guide](./Windows_Local_File_Assistant_Guide.docx): historical only.
  Its single-script commands, Python 3.11 minimum, opening files, grouping,
  external trash, and deferred tests/recovery conflict with the current contract.

Development follows tests first, using temporary fixtures. Local AI proposes
filename-only organization; it has no filesystem, approval or shell tools.
