"""Una sola interfaz de baseline/estado para dos mecanismos distintos de "¿está completo?".

  * ManifestBaselineSource    -> layout con manifest.json (USB de ambas ediciones, remoto rsync).
                                 Completo = manifest `completed` del mismo motor (model/manifest.py).
  * ReceivedUuidBaselineSource -> árbol remoto de btrfs (<base>/{root,home}/<nombre>, SIN manifest).
                                 Completo = subvolumen de solo lectura CON received_uuid; una copia
                                 a medias de un `btrfs receive` interrumpido es lectura-escritura y
                                 no tiene received_uuid.

Ninguno inventa manifests para el otro: el remoto btrfs existente se lee tal cual está.

Contrato común (`BaselineSource`):
  baseline(kind) -> Baseline | None     None = el envío tendría que ser COMPLETO.
  statuses(kind) -> {nombre: "completed" | "partial"}
  Si el origen no se puede consultar lanza RemoteQueryError (nunca devuelve "vacío").
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..model.manifest import COMPLETED, PARTIAL, SnapshotRecord, pick_baseline

ORIGIN_MANIFEST = "manifest"
ORIGIN_RECEIVED_UUID = "received_uuid"


@dataclass(frozen=True)
class Baseline:
    name: str  # nombre del snapshot / subvolumen que sirve de base
    origin: str  # ORIGIN_MANIFEST | ORIGIN_RECEIVED_UUID
    kind: Optional[str] = None  # root/home para btrfs; None para rsync
    local_path: Optional[Path] = None  # btrfs: el padre `-p` local
    manifest: Optional[dict] = None  # solo cuando existe un manifest real


class BaselineSource(ABC):
    origin: str = "?"

    @abstractmethod
    def baseline(self, kind: Optional[str] = None) -> Optional[Baseline]: ...

    @abstractmethod
    def statuses(self, kind: Optional[str] = None) -> Dict[str, str]: ...

    def would_be_full(self, kinds: Iterable[Optional[str]] = (None,)) -> List[Optional[str]]:
        """Las `kinds` cuyo envío sería completo."""
        return [k for k in kinds if self.baseline(k) is None]


class ManifestBaselineSource(BaselineSource):
    """Baseline desde manifests reales. `parent_exists(kind, record)` deja que btrfs exija que el
    padre local siga existiendo."""

    origin = ORIGIN_MANIFEST

    def __init__(self, records: Iterable[SnapshotRecord], engine: str, local_parent=None):
        self._records = list(records)
        self._engine = engine
        self._local_parent = local_parent  # callable(kind, record) -> Optional[Path]

    def baseline(self, kind: Optional[str] = None) -> Optional[Baseline]:
        rec = pick_baseline(self._records, self._engine)
        if rec is None:
            return None
        if kind is None or self._local_parent is None:
            return Baseline(rec.name, ORIGIN_MANIFEST, kind, None, rec.manifest)
        parent = self._local_parent(kind, rec)
        if parent is None:
            return None  # sin padre local no hay envío incremental posible
        return Baseline(parent.name, ORIGIN_MANIFEST, kind, parent, rec.manifest)

    def statuses(self, kind: Optional[str] = None) -> Dict[str, str]:
        return {
            r.name: (COMPLETED if r.status == COMPLETED else PARTIAL)
            for r in self._records
            if r.engine == self._engine
        }
