import os
import stat
from pathlib import Path

# Define the sandbox directory
SANDBOX_DIR = Path("C:/AssistantSandbox")


def check_sandbox(directory_path: Path) -> bool:
    """
    Check whether the supplied path exists and is a directory
    """
    return directory_path.exists() and directory_path.is_dir()


def _validate_windows_component(part: str) -> None:
    """Reject names that Windows treats as invalid or special devices."""
    base = part.split(".", 1)[0].rstrip(" ").casefold()
    device_name = base in {"con", "prn", "aux", "nul", "conin$", "conout$"}
    device_name |= (
        len(base) == 4
        and base[:3] in {"com", "lpt"}
        and base[3] in "123456789¹²³"
    )
    if (
        part.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
        or device_name
    ):
        raise ValueError(f"Invalid Windows path component: {part!r}")


def _inspect_component(component: Path) -> os.stat_result:
    """Inspect a path entry without accepting a filesystem redirect."""
    if component.is_symlink():
        raise ValueError("Symbolic links are not allowed.")
    if component.is_junction():
        raise ValueError("Junctions are not allowed.")
    details = component.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("Reparse points are not allowed.")
    return details


def validate_workspace(root: Path) -> Path:
    """Return a canonical existing drive-letter directory without redirects.

    Drive-letter syntax does not certify fixed NTFS storage or exclude cloud
    providers. Validation cannot prevent concurrent path substitution.
    """
    if not root.is_absolute():
        raise ValueError("Workspace must be absolute.")
    if os.name == "nt" and not (
        len(root.drive) == 2 and root.drive[0].isalpha() and root.drive[1] == ":"
    ):
        raise ValueError("Workspace must be on a local drive.")

    for part in root.parts[1:]:
        if part != "..":
            _validate_windows_component(part)

    # Inspect from the volume root down, before any operation follows a redirect.
    for component in (*reversed(root.parents), root):
        details = _inspect_component(component)

    if not stat.S_ISDIR(details.st_mode):
        raise NotADirectoryError(f"Workspace is not a directory: {root}")

    return root.resolve(strict=True)


def validate_path(workspace: Path, user_path: str) -> Path:
    """Return a canonical existing path within a validated workspace.

    Callers must revalidate before use; concurrent substitution remains possible.
    """
    if user_path == "":
        raise ValueError("User path must not be empty.")
    path_object = Path(user_path)

    if path_object.drive or path_object.root:
        raise ValueError("Path must be relative.")

    if ".." in path_object.parts:
        raise ValueError("Parent traversal is not allowed.")

    for part in path_object.parts:
        _validate_windows_component(part)
    if path_object.parts and path_object.parts[0].casefold() == "trash":
        raise ValueError("Trash is reserved")

    canonical_root = validate_workspace(workspace)
    candidate = workspace / user_path

    current = workspace
    for part in path_object.parts:
        current = current / part
        _inspect_component(current)

    canonical_path = candidate.resolve(strict=True)
    try:
        relative = canonical_path.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError("Path resolves outside the workspace.") from exc
    if relative.parts and relative.parts[0].casefold() == "trash":
        raise ValueError("Trash is reserved")
    return canonical_path


def validate_destination(
    workspace: Path, user_path: str, *, planned_folder: str | None = None,
) -> Path:
    """Validate an absent destination without creating or reserving it.

    Normally the parent must exist. An explicitly supplied planned_folder is
    one absent child of the root, and user_path must name a file directly in
    that folder. An occupied planned folder requires fresh validation without
    this option. The caller supplies reviewed intent; this option does not
    confer execution authorization. validate_path remains existing-only.

    Later execution must revalidate root/parent identity, destination absence,
    and source identity, type, size, timestamps and link count. Returning a
    Path does not prevent concurrent substitution or authorize an overwrite.
    """
    if user_path == "":
        raise ValueError("User path must not be empty.")
    # Path normalizes terminal dots and separators; neither names a new child.
    if user_path.replace("\\", "/").split("/")[-1] in {"", ".", ".."}:
        raise ValueError("Destination must name a new child.")
    path_object = Path(user_path)
    if path_object.drive or path_object.root:
        raise ValueError("Path must be relative.")
    if ".." in path_object.parts:
        raise ValueError("Parent traversal is not allowed.")
    for part in path_object.parts:
        _validate_windows_component(part)
    if path_object.parts[0].casefold() == "trash":
        raise ValueError("Trash is reserved")

    if planned_folder is not None:
        if (planned_folder in {"", ".", ".."}
                or any(separator in planned_folder for separator in "/\\")):
            raise ValueError("Planned folder must be one new root child.")
        _validate_windows_component(planned_folder)
        if (len(path_object.parts) != 2
                or path_object.parts[0].casefold() != planned_folder.casefold()):
            raise ValueError("Destination must be directly inside the planned folder.")
        # Validate the proposed folder itself against the existing root. Do not
        # resolve the missing folder/file or relax existing-path validation.
        folder = validate_destination(workspace, planned_folder)
        return folder / path_object.name

    parent = validate_path(workspace, str(path_object.parent))
    if not stat.S_ISDIR(_inspect_component(parent).st_mode):
        raise NotADirectoryError(f"Destination parent is not a directory: {parent}")
    destination = parent / path_object.name
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"Destination already exists: {destination}")
    # Refuse case variants even on directories with case-sensitive lookup.
    if any(child.name.casefold() == path_object.name.casefold()
           for child in parent.iterdir()):
        raise FileExistsError(f"Destination already exists: {destination}")
    return destination
