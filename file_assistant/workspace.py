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
