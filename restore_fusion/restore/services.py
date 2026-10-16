"""Servicios de la pantalla de restauración: qué hay para restaurar y cómo dejarlo navegable.

Reúne (sin UI) los escáneres de `sources.py`, el montaje sshfs de solo lectura y el staging btrfs.
La app solo conoce esta interfaz, así que las pruebas la sustituyen por un doble.

  local_snapshots()  -> rápido: USB (ambas carpetas de motor) + /.snapshots; sin red
  remote_snapshots() -> lento (red): host remoto, ambos layouts; devuelve (snapshots, avisos)
  prepare(origin, area, progress) -> carpeta navegable (TREE tal cual; REMOTE: sshfs ro, o staging;
                                     STREAM: staging)
"""

import getpass
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .. import drives
from ..config.schema import Config
from ..engines.btrfs.staging import Staging
from ..model.snapshots import Area, Origin, Snapshot, Storage, merge_timeline
from . import sources
from .remote_mount import RemoteMounter


def usb_mount(
    cfg: Config, *, is_mount=drives._real_ismount, bases=None
) -> Tuple[Optional[Path], str]:
    """(punto de montaje del USB, motivo si no hay). Un directorio que no es punto de montaje real
    NO se lee como si fuera la unidad (regla 1)."""
    if cfg.target_dir:
        mount = drives.split_target(cfg.target_dir, "rsync")[0]
        if mount.is_dir() and is_mount(mount):
            return mount, ""
        return None, f"USB: {mount} is not mounted"
    for engine in ("rsync", "btrfs"):
        found = drives.autodetect_target(
            cfg.user, engine, cfg.target_label, bases=bases, is_mount=is_mount
        )
        if found:
            return Path(found), ""
    return None, "USB: no backup drive found"


class RestoreServices:
    def __init__(
        self,
        cfg: Config,
        *,
        mounter: Optional[RemoteMounter] = None,
        staging: Optional[Staging] = None,
        is_mount=drives._real_ismount,
        bases=None,
        scan_remote=sources.scan_remote_btrfs,
    ):
        self.cfg = cfg
        self.user = cfg.user or os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        self.home = Path(cfg.home)
        self.mounter = mounter or RemoteMounter()
        self.staging = staging or Staging(cfg)
        self._is_mount, self._bases, self._scan_remote = is_mount, bases, scan_remote
        self.notes: List[str] = []

    # -- rápido ----------------------------------------------------------------------------
    def local_snapshots(self) -> List[Snapshot]:
        self.notes = []
        out: List[Snapshot] = []
        mount, why = usb_mount(self.cfg, is_mount=self._is_mount, bases=self._bases)
        if mount is None:
            self.notes.append(why)
        else:
            for engine, dirname in drives.ENGINE_DIRNAMES.items():
                tree = mount / dirname / "snapshots"
                if tree.is_dir():
                    out += sources.scan_tree(
                        tree, label="USB", kind="usb", user=self.user, home=self.home
                    )
        out += sources.scan_local(
            Path(self.cfg.local_snapshots_dir), user=self.user, home=self.home
        )
        return merge_timeline(out)

    # -- lento (red) ---------------------------------------------------------------------------
    def remote_snapshots(self) -> Tuple[List[Snapshot], List[str]]:
        cfg = self.cfg
        notes: List[str] = []
        if not cfg.remote_configured:
            return [], notes
        label = cfg.remote_name or cfg.remote_host
        found: List[Snapshot] = []
        # layout rsync (restore-tui/snapshots/<ts>/home): se monta de solo lectura y se lee como un USB
        mnt = self.mounter.mount(
            user=cfg.remote_user or self.user, host=cfg.remote_host_lan or cfg.remote_host,
            remote_path=cfg.remote_root("rsync"), port=cfg.remote_port, slug="tree", home=self.home,
        )  # fmt: skip
        if mnt is not None and (mnt / "snapshots").is_dir():
            found += sources.scan_tree(mnt / "snapshots", label=label, kind="remote", user=self.user,
                                       home=self.home, mount=mnt)  # fmt: skip
        # layout btrfs (<base>/{root,home}/<ts_compact>): lista por ssh, sin manifest
        snaps, status, skipped = self._scan_remote(
            host=cfg.remote_host_lan or cfg.remote_host, user=cfg.remote_user or self.user,
            port=cfg.remote_port, base=cfg.remote_root("btrfs"), label=label, local_user=self.user,
            home=self.home, local_dir=Path(cfg.local_snapshots_dir), ssh_home=self.home,
        )  # fmt: skip
        found += snaps
        if status == sources.RemoteStatus.UNREACHABLE and mnt is None:
            notes.append(f"{label} not responding (check network / SSH / config)")
        elif status == sources.RemoteStatus.NO_PRIVILEGE:
            notes.append(
                f"{label} connected, but 'sudo btrfs' is not allowed there - listing names only"
            )
        elif skipped:
            notes.append(f"{label}: {skipped} snapshot(s) are already local")
        return found, notes

    # -- dejar navegable -----------------------------------------------------------------------------------
    def prepare(
        self, origin: Origin, area: Area, progress: Optional[Callable[[str], None]] = None
    ) -> Path:
        if area.storage is Storage.TREE:
            return Path(str(area.path))
        if area.storage is Storage.REMOTE:
            cfg = self.cfg
            mnt = self.mounter.mount(
                user=cfg.remote_user or self.user, host=cfg.remote_host_lan or cfg.remote_host,
                remote_path=str(area.path), port=cfg.remote_port, slug=f"{area.kind}_{Path(str(area.path)).name}",
                home=self.home,
            )  # fmt: skip
            if mnt is not None:
                user_dir = mnt / self.user
                return user_dir if (area.home_user_subdir and user_dir.is_dir()) else mnt
        self.staging._progress_cb = progress
        return self.staging.deploy(area, self.user)

    def cancel(self) -> None:
        self.staging.cancel()

    def cleanup(self) -> None:
        for fn in (self.staging.cleanup_own, self.mounter.cleanup):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
