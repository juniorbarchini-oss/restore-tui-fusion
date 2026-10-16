"""Modelo de lo que se puede restaurar: snapshots con uno o más ORÍGENES (USB, remoto, local) y,
en cada origen, una o más ÁREAS navegables (Home, Root /).

Un mismo backup que está en el USB y en el host remoto es UNA entrada con las dos etiquetas
(`[USB+Box]`), no dos (comportamiento de restore-tui, ahora para ambos motores).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union

ORDER = {"local": 0, "usb": 1, "remote": 2}  # de dónde se lee primero: lo más barato
_COMPACT_TS = re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{6})")


class Storage(str, Enum):
    TREE = "tree"  # directorio navegable ya montado (USB, local, árbol sshfs)
    STREAM = "stream"  # .btrfs.zst: hay que recibirlo a staging (btrfs) antes de navegar
    REMOTE = "remote"  # subvolumen btrfs en el host: sshfs de solo lectura, o staging


@dataclass(frozen=True)
class Area:
    kind: str  # "home" | "root" | "other"
    label: str  # "Home" / "Root /"
    path: Union[Path, str]  # Path (TREE/STREAM) o ruta en el host (REMOTE)
    target_base: Path  # "restaurar al original": rel. a esta área cae bajo target_base
    storage: Storage = Storage.TREE
    home_user_subdir: bool = False  # el área es <subvol>/<usuario>/ y se entra a esa carpeta

    @property
    def is_system(self) -> bool:
        return self.kind != "home"


@dataclass
class Origin:
    label: str  # "USB", nombre del remoto, "Local"
    kind: str  # "usb" | "remote" | "local"
    areas: List[Area] = field(default_factory=list)
    engine: Optional[str] = None
    mount: Optional[Path] = None  # raíz montada (sshfs) si el árbol vino de un montaje

    def area(self, kind: str) -> Optional[Area]:
        return next((a for a in self.areas if a.kind == kind), None)


@dataclass
class Snapshot:
    key: str  # "YYYY-MM-DD_HHMMSS[_N]" (o id local) - la clave de fusión
    timestamp: datetime
    status: str = "completed"  # completed | partial | unknown
    origins: List[Origin] = field(default_factory=list)
    title: str = ""  # texto extra (p. ej. Snapper #3)

    @property
    def display(self) -> str:
        return self.title or self.key.replace("_", " ")

    @property
    def tag(self) -> str:
        return "+".join(o.label for o in self.origins)

    @property
    def usable(self) -> bool:
        return self.status in ("completed", "partial", "unknown")

    def best_origin(self) -> Optional[Origin]:
        return min(self.origins, key=lambda o: ORDER.get(o.kind, 9), default=None)


def key_from_compact(compact: str) -> Optional[str]:
    """'20260915_001833' -> '2026-09-15_001833'."""
    m = _COMPACT_TS.fullmatch(compact)
    return f"{m[1]}-{m[2]}-{m[3]}_{m[4]}" if m else None


def parse_key_time(key: str) -> Optional[datetime]:
    try:
        return datetime.strptime(key[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None


def merge_timeline(*lists: List[Snapshot]) -> List[Snapshot]:
    """Une las listas: la misma clave es una sola entrada con todos sus orígenes. Más nuevo primero.
    Un estado `partial` de cualquier origen manda sobre `completed` (se avisa, no se oculta)."""
    merged: Dict[str, Snapshot] = {}
    for lst in lists:
        for s in lst:
            cur = merged.get(s.key)
            if cur is None:
                merged[s.key] = Snapshot(s.key, s.timestamp, s.status, list(s.origins), s.title)
                continue
            cur.origins.extend(s.origins)
            if "partial" in (cur.status, s.status):
                cur.status = "partial"
            elif "unknown" in (cur.status, s.status) and cur.status != "completed":
                cur.status = "unknown"
    return sorted(merged.values(), key=lambda s: (s.timestamp, s.key), reverse=True)
