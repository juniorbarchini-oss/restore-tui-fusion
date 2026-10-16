"""Modelos del lado restauración (de btrfs-restore-tui/models.py)."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class ConflictResolution(str, Enum):
    OVERWRITE = "overwrite"
    BACKUP = "backup"
    SKIP = "skip"
    CANCEL = "cancel"


@dataclass
class RestoreItem:
    source_path: Path
    is_dir: bool
    size_bytes: int
    rel_path: Path  # relativo a la raíz del snapshot (p. ej. Documents/file.md)

    @property
    def formatted_size(self) -> str:
        if self.size_bytes < 1024:
            return f"{self.size_bytes} B"
        if self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.1f} KB"
        if self.size_bytes < 1024 * 1024 * 1024:
            return f"{self.size_bytes / (1024 * 1024):.1f} MB"
        return f"{self.size_bytes / (1024 * 1024 * 1024):.2f} GB"


@dataclass
class RestorePlan:
    items: List[RestoreItem] = field(default_factory=list)
    target_base: Path = field(default_factory=Path.home)
    resolution: ConflictResolution = ConflictResolution.BACKUP

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def total_items(self) -> int:
        return len(self.items)


@dataclass
class RestoreProgress:
    total_files: int
    processed_files: int
    total_bytes: int
    processed_bytes: int
    current_file: str
    spinner_idx: int = 0
    done: bool = False
    error: Optional[str] = None
    failed_files: int = 0
    errors: List[str] = field(default_factory=list)
    fatal: bool = False  # abortó antes de terminar (vs. algunos archivos fallaron)

    @property
    def percent(self) -> int:
        if self.done and not self.error:
            return 100
        if self.total_bytes == 0:
            return 100 if self.done else 0
        return min(100, int((self.processed_bytes / self.total_bytes) * 100))

    @property
    def ok(self) -> bool:
        """Solo True si terminó y NO hubo ningún error ni abort."""
        return self.done and not self.fatal and not self.error and self.failed_files == 0
