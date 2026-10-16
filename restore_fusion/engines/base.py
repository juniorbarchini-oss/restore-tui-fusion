"""Interfaz del motor de copia (CÓMO se copia): rsync+hardlinks o btrfs send/receive.

El motor no sabe dónde vive el destino (eso es `destinations/`) ni qué paquetes reinstalar
(eso es `packages/`). Recibe un destino y un contexto, copia, y devuelve un resultado.

Regla 2 del README, en la interfaz: `run()` termina en completed | partial | failed y SOLO
un `completed` mueve `latest` o puede ser baseline.
"""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..destinations.base import Destination

Emit = Callable[[str, str], None]  # (tipo_de_evento, mensaje)


def no_emit(_t: str, _m: str) -> None:
    pass


class FullCheck(enum.Enum):
    YES = "yes"  # la corrida sería completa (sin baseline)
    NO = "no"  # incremental
    UNKNOWN = "unknown"  # no se pudo preguntar: NUNCA se lee como "completa/vacía"


@dataclass
class EngineContext:
    source_home: Path
    user: str
    exclusions: List[str] = field(default_factory=list)
    stage_dir: Optional[Path] = None  # system state ya preparado; el motor lo sube al snapshot
    hostname: str = ""


@dataclass
class EngineResult:
    status: str  # completed | partial | failed
    destination: str = ""
    snapshot_name: str = ""
    snapshot_path: Optional[Path] = None
    baseline: Optional[str] = None
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)
    message: str = ""
    details: Dict[str, object] = field(default_factory=dict)  # p. ej. rsync_exit

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class CopyEngine(ABC):
    id: str = "?"

    @abstractmethod
    def available(self) -> Tuple[bool, str]:
        """¿Están las herramientas? (rsync / btrfs-progs + root)."""

    @abstractmethod
    def would_be_full(self, dest: Destination) -> FullCheck: ...

    @abstractmethod
    def run(
        self, dest: Destination, ctx: EngineContext, emit: Emit = no_emit, dry_run: bool = False
    ) -> EngineResult: ...
