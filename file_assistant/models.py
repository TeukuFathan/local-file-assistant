"""Small immutable contracts shared by the file assistant layers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Entry:
    relative_path: str
    kind: Literal["file", "directory"]

    def __post_init__(self) -> None:
        if self.kind not in ("file", "directory"):
            raise ValueError("Entry kind must be 'file' or 'directory'.")


@dataclass(frozen=True)
class ScanResult:
    entries: tuple[Entry, ...]
    errors: tuple[str, ...]
    cancelled: bool


Action = Literal["mkdir", "move", "trash", "restore"]


@dataclass(frozen=True)
class OperationRequest:
    action: Action
    source: str | None = None
    destination: str | None = None
    trash_id: str | None = None

    def __post_init__(self) -> None:
        requirements = {
            "mkdir": (False, True, False),
            "move": (True, True, False),
            "trash": (True, False, False),
            "restore": (False, False, True),
        }
        if self.action not in requirements:
            raise ValueError("Unsupported operation action.")
        fields = (self.source, self.destination, self.trash_id)
        required = requirements[self.action]
        if any(bool(value) != needed for value, needed in zip(fields, required)):
            raise ValueError(f"Invalid fields for {self.action} request.")
        for path in (self.source, self.destination):
            if path is not None and (Path(path).is_absolute() or Path(path).drive):
                raise ValueError("Operation paths must be relative.")


@dataclass(frozen=True)
class PathObservation:
    relative_path: str
    present: bool
    device: int | None = None
    file_id: int | None = None
    size: int | None = None
    modified_ns: int | None = None
    kind: Literal["file", "directory"] | None = None
    link_count: int | None = None
    parent_device: int | None = None
    parent_file_id: int | None = None


@dataclass(frozen=True)
class OperationPlan:
    plan_id: str
    root: Path
    root_identity: tuple[int, int]
    request: OperationRequest
    observations: tuple[PathObservation, ...]


OperationStatus = Literal[
    "success", "rejected", "cancelled", "failed", "recovery_required"
]


@dataclass(frozen=True)
class OperationResult:
    plan_id: str
    status: OperationStatus
    message: str
    trash_id: str | None = None
