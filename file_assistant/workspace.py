from pathlib import Path

# Define the sandbox directory
SANDBOX_DIR = Path("C:/AssistantSandbox")


def check_sandbox(directory_path: Path) -> bool:
    """
    Check whether the supplied path exists and is a directory
    """
    return directory_path.exists() and directory_path.is_dir()


def validate_path(workspace: Path, user_path: str) -> Path: 
    # convert user path into a Path object 
    path_object: Path = Path(user_path)

    if path_object.drive or path_object.root:
        raise ValueError("Path must be relative.")

    # "in" like .contains() in java 
    if ".." in path_object.parts:
        raise ValueError("Parent traversal is not allowed.")
    
    if path_object.parts:
        if path_object.parts[0].casefold() == "trash":
            raise ValueError("Trash is reserved")

    candidate: Path = workspace / user_path

    # Symlink is essentially just shortcut but on the os level
    current = workspace

    if current.is_symlink():
        raise ValueError("Symbolic links are not allowed.")

    if current.is_junction():
        raise ValueError("Junctions are not allowed.")
    
    for part in path_object.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are not allowed.")
        if current.is_junction():
            raise ValueError("Junctions are not allowed.")

    if not candidate.exists():
        raise FileNotFoundError(" Path does not exist") 
