"""Servicios de la pantalla de restauración desde PBS: snapshots del servidor como `Snapshot`s y
montajes FUSE de solo lectura bajo demanda. Misma interfaz que `restore.services.RestoreServices`,
así que `ui.restore_app.RestoreApp` (motor de restauración compartido, diálogos, lista completa de
errores) se usa tal cual: no hay un segundo explorador.

Limpieza: se desmonta y se usa `rmdir` (jamás rmtree sobre algo que pueda seguir montado).
"""

import os
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..model.snapshots import Area, Origin, Snapshot, Storage
from .client import PbsClient, PbsError, PbsSnapshot

LABEL = "PBS"


def split_area_path(path: str) -> Tuple[str, str]:
    """'host/<id>/<ts>/root.pxar' -> ('host/<id>/<ts>', 'root.pxar')."""
    snap, _, archive = str(path).rpartition("/")
    return snap, archive


class PbsServices:
    def __init__(self, cfg, client: PbsClient, hostname: str, *, base: Optional[Path] = None,
                 only: Optional[PbsSnapshot] = None):  # fmt: skip
        self.cfg, self.client, self.hostname, self.only = cfg, client, hostname, only
        self.home = Path(cfg.home)
        self.base = (
            Path(base) if base else Path(tempfile.mkdtemp(prefix=f"pbs-restore-{os.getpid()}-"))
        )
        self._mounted: Dict[str, Path] = {}  # "<snap>/<archive>" -> punto de montaje
        self._pbs: List[PbsSnapshot] = []
        self.notes: List[str] = []

    def _areas(self, snap: PbsSnapshot, archives: List[str]) -> List[Area]:
        has_root = "root.pxar" in archives
        areas = []
        for name in ("root.pxar", "home.pxar"):
            if name not in archives:
                continue
            kind = name.split(".")[0]
            areas.append(
                Area(
                    kind=kind,
                    label=("Root /" if kind == "root" else "Home"),
                    path=f"{snap.path}/{name}",
                    target_base=(Path("/") if kind == "root" else self.home),
                    storage=Storage.REMOTE,  # se monta al abrir (muestra el diálogo de progreso)
                    # un snapshot con root.pxar viene del motor btrfs: home.pxar es el subvolumen /home
                    # y el usuario es una carpeta dentro. Sin root.pxar (motor rsync) es el $HOME mismo.
                    home_user_subdir=(kind == "home" and has_root),
                )
            )
        return areas

    # -- interfaz de RestoreServices ------------------------------------------------------------------
    def local_snapshots(self) -> List[Snapshot]:
        """Lista el servidor (una consulta de red). Un fallo se propaga: no se lee como 'vacío'."""
        self._pbs = [self.only] if self.only else self.client.list_snapshots(self.hostname)
        out = []
        for s in self._pbs:
            areas = self._areas(s, self.client.archives(s.path))
            if not areas:
                continue
            origin = Origin(label=LABEL, kind="remote", areas=areas)
            out.append(Snapshot(s.key, s.local_time, "completed", [origin]))
        return out

    def remote_snapshots(self) -> Tuple[List[Snapshot], List[str]]:
        return [], []

    def prepare(self, origin: Origin, area: Area, progress: Optional[Callable] = None) -> Path:
        snap_path, archive = split_area_path(str(area.path))
        key = f"{snap_path}/{archive}"
        mnt = self._mounted.get(key)
        if mnt is None:
            mnt = self.base / f"{len(self._mounted)}-{area.kind}"
            self.client.mount(snap_path, archive, mnt)
            self._mounted[key] = mnt
        user_dir = mnt / self.cfg.user
        if area.home_user_subdir and user_dir.is_dir():
            return user_dir
        return mnt

    def cancel(self) -> None:
        pass

    def cleanup(self) -> None:
        for mnt in list(self._mounted.values()):
            self.client.unmount(mnt)
        self._mounted.clear()
        try:
            self.base.rmdir()
        except OSError:
            pass
