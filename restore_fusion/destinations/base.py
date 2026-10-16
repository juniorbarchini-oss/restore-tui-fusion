"""Destinos de backup: DÓNDE vive el árbol de snapshots (no CÓMO se copia).

El motor de copia (rsync / btrfs) vive en `engines/`; un destino solo sabe hablar con un
lugar: listar/crear/leer/borrar snapshots, mover `latest`, correr un comando ahí.

Reglas:
  * `reachable()` y `ensure_ready()` de un destino local exigen un punto de montaje REAL.
  * Un snapshot solo se borra con un `Approval` que lo cubra (safety/approval.py).
  * `latest` solo se mueve a un snapshot `completed`.
  * Una consulta remota que FALLA lanza RemoteQueryError; nunca se lee como "vacío".

Layout de snapshots, igual en todo destino de árbol:
    <root>/snapshots/<YYYY-MM-DD_HHMMSS>/...      <root>/latest -> snapshots/<nombre>
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..model.manifest import COMPLETED, is_valid_snapshot_name, may_update_latest, status_of
from ..safety.approval import Approval, ApprovalError

SUFFIXES = ("", *(f"_{n}" for n in range(2, 10)))


class RemoteQueryError(RuntimeError):
    """No se pudo PREGUNTAR al remoto (timeout, ssh caído, sudo). Distinto de "pregunté y está
    vacío": quien llama NO debe caer a un backup completo por esto."""


class Destination(ABC):
    label: str = "?"

    # -- disponibilidad ----------------------------------------------------
    @abstractmethod
    def reachable(self) -> Tuple[bool, str]:
        """(ok, motivo). Chequeo barato: ¿el disco está montado de verdad / el host responde?"""

    @abstractmethod
    def ensure_ready(self) -> Tuple[bool, str]:
        """Crea snapshots/ y confirma que se puede escribir."""

    @abstractmethod
    def display_root(self) -> str: ...

    def snapshot_fs_path(self, snap: str) -> Optional[Path]:
        """Ruta local del snapshot, o None si el destino no está en este filesystem."""
        return None

    # -- snapshots ---------------------------------------------------------------
    @abstractmethod
    def list_snapshots(self) -> List[str]:
        """Nombres de snapshots reales (regex, sin symlinks)."""

    @abstractmethod
    def list_entries(self, snap: str) -> List[str]:
        """Nombres dentro del directorio de un snapshot (para clasificar manifests v1)."""

    @abstractmethod
    def read_manifest(self, snap: str) -> dict: ...

    @abstractmethod
    def write_manifest(self, snap: str, data: dict) -> None: ...

    @abstractmethod
    def reserve_snapshot(self, name: str) -> str:
        """Crea snapshots/<name>; si choca prueba _2.._9. Devuelve el nombre creado."""

    @abstractmethod
    def push_meta(self, stage_dir: Path, snap: str) -> None:
        """Copia un directorio preparado localmente a snapshots/<snap>/."""

    @abstractmethod
    def update_latest(self, snap: str) -> None:
        """Mueve `latest`. Solo a un snapshot cuyo manifest diga `completed`."""

    @abstractmethod
    def remove(self, snap: str, approval: Optional[Approval]) -> None:
        """Borra un snapshot. Sin Approval que lo cubra: ApprovalError y no se borra nada."""

    @abstractmethod
    def write_kit_files(self, files: Dict[str, bytes]) -> None:
        """Deja {ruta_relativa: contenido} en la raíz del destino (kit de recuperación)."""

    @abstractmethod
    def run_cmd(self, argv: Sequence[str], **kw):
        """Corre un comando en el destino (siempre argv)."""

    # -- extremos para rsync (los usa el motor rsync) ----------------------------------------
    @abstractmethod
    def tree_path(self, snap: str) -> str:
        """Ruta del snapshot tal como la ve el lado del destino (para --link-dest y el destino)."""

    def rsync_transport(self) -> List[str]:
        """Argumentos de transporte de rsync (vacío en local; ['-e', 'ssh ...'] en SSH)."""
        return []

    def spec(self, path: str) -> str:
        """Ruta como argumento de rsync (local: tal cual; SSH: user@host:ruta)."""
        return path

    # -- retención ---------------------------------------------------------------------
    @abstractmethod
    def usage_summary(self) -> str: ...

    @abstractmethod
    def should_prune_more(self, completed_count: int, min_keep: int) -> bool:
        """True mientras el destino siga sobre presupuesto (nunca bajo el piso)."""

    # -- helpers compartidos -----------------------------------------------------------------
    def _check_removal(self, snap: str, approval: Optional[Approval]) -> None:
        if not is_valid_snapshot_name(snap):
            raise ValueError(f"refusing to remove non-snapshot path: {snap!r}")
        if approval is None or not approval.covers("delete", snap):
            raise ApprovalError(f"deletion of {snap} refused: not approved")

    def _check_latest(self, snap: str) -> None:
        if not is_valid_snapshot_name(snap):
            raise ValueError(f"invalid snapshot name: {snap!r}")
        status = status_of(self.read_manifest(snap))
        if not may_update_latest(status):
            raise ValueError(f"'latest' only moves to a completed snapshot ({snap} is {status})")


__all__ = ["Destination", "RemoteQueryError", "SUFFIXES", "COMPLETED"]
