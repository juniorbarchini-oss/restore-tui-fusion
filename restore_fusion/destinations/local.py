"""Destino local: una unidad montada (USB). `root` = <mount>/<carpeta del motor>.

De restore-tui/destinations.py (LocalDestination) + el chequeo de punto de montaje de
btrfs-restore-tui/backup.py (_target_writable): un directorio de montaje que quedó tras
desenchufar el disco es escribible pero vive en el disco raíz; NO es un destino válido.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..model.manifest import is_valid_snapshot_name
from ..safety.approval import Approval
from .base import SUFFIXES, Destination

IsMount = Callable[[Path], bool]


def _real_ismount(p: Path) -> bool:
    return os.path.ismount(str(p))


class LocalDestination(Destination):
    def __init__(
        self,
        root: Path,
        *,
        max_disk_percent: int = 80,
        label: str = "USB",
        is_mount: IsMount = _real_ismount,
        rmtree: Callable[[Path], None] = shutil.rmtree,
    ):
        self.root = Path(root)
        self.mount = self.root.parent
        self.snaps = self.root / "snapshots"
        self.latest = self.root / "latest"
        self.max_disk_percent = max_disk_percent
        self.label = label
        self._is_mount = is_mount
        self._rmtree = rmtree

    # -- disponibilidad ---------------------------------------------------------
    def reachable(self) -> Tuple[bool, str]:
        if not self.mount.is_dir():
            return False, f"{self.mount} is not mounted"
        if not self._is_mount(self.mount):
            return False, f"{self.mount} is not a mount point (drive unplugged?)"
        return True, str(self.mount)

    def ensure_ready(self) -> Tuple[bool, str]:
        ok, why = self.reachable()  # se repite justo antes de escribir
        if not ok:
            return False, why
        try:
            self.snaps.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".restore-fusion-write-test"
            probe.write_text("ok")
            probe.unlink()
            return True, "writable"
        except OSError as exc:
            return False, f"not writable: {exc}"

    def display_root(self) -> str:
        return str(self.snaps)

    def snapshot_fs_path(self, snap: str) -> Optional[Path]:
        return self.snaps / snap

    def tree_path(self, snap: str) -> str:
        return str(self.snaps / snap)

    # -- snapshots --------------------------------------------------------------------
    def list_snapshots(self) -> List[str]:
        if not self.snaps.is_dir():
            return []
        return sorted(
            e.name
            for e in self.snaps.iterdir()
            if e.is_dir() and not e.is_symlink() and is_valid_snapshot_name(e.name)
        )

    def list_entries(self, snap: str) -> List[str]:
        try:
            return sorted(p.name for p in (self.snaps / snap).iterdir())
        except OSError:
            return []

    def read_manifest(self, snap: str) -> dict:
        try:
            data = json.loads((self.snaps / snap / "manifest.json").read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_manifest(self, snap: str, data: dict) -> None:
        (self.snaps / snap / "manifest.json").write_text(json.dumps(data, indent=2))

    def reserve_snapshot(self, name: str) -> str:
        ok, why = self.ensure_ready()
        if not ok:
            raise OSError(why)
        for suffix in SUFFIXES:
            cand = self.snaps / f"{name}{suffix}"
            try:
                cand.mkdir(parents=False, exist_ok=False)
                return cand.name
            except FileExistsError:
                continue
        raise OSError(f"too many snapshots for {name}")

    def push_meta(self, stage_dir: Path, snap: str) -> None:
        dest = self.snaps / snap
        for item in Path(stage_dir).iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
                if item.suffix == ".sh":
                    target.chmod(0o755)

    def update_latest(self, snap: str) -> None:
        self._check_latest(snap)
        tmp = self.root / ".latest.tmp"
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            tmp.symlink_to(Path("snapshots") / snap)
            os.replace(tmp, self.latest)  # atómico: nunca queda sin `latest` a medias
        except OSError as exc:
            raise OSError(f"could not update 'latest': {exc}") from exc

    def remove(self, snap: str, approval: Optional[Approval]) -> None:
        self._check_removal(snap, approval)
        self._rmtree(self.snaps / snap)

    def write_kit_files(self, files: Dict[str, bytes]) -> None:
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if rel.endswith(".sh"):
                path.chmod(0o755)

    def run_cmd(self, argv: Sequence[str], **kw):
        return subprocess.run(list(argv), capture_output=True, text=True, **kw)

    # -- retención ----------------------------------------------------------------------
    def _disk_percent(self) -> Optional[float]:
        try:
            u = shutil.disk_usage(self.root)
            return u.used * 100.0 / u.total if u.total else 0.0
        except OSError:
            return None

    def usage_summary(self) -> str:
        pct = self._disk_percent()
        return "drive usage unknown" if pct is None else f"drive {pct:.0f}% full"

    def should_prune_more(self, completed_count: int, min_keep: int) -> bool:
        pct = self._disk_percent()
        if pct is None:
            return False
        return pct > self.max_disk_percent and completed_count > max(1, min_keep)
