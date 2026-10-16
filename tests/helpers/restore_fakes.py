"""Doble de RestoreServices para probar la pantalla de restauración sin red ni discos reales."""

from datetime import datetime
from pathlib import Path

from restore_fusion.model.snapshots import Area, Origin, Snapshot, Storage


class FakeServices:
    def __init__(self, local=(), remote=(), notes=(), prepare=None):
        self._local, self._remote, self._notes = list(local), list(remote), list(notes)
        self._prepare = prepare
        self.prepared = []
        self.cancelled = False
        self.cleaned = 0

    def local_snapshots(self):
        return list(self._local)

    def remote_snapshots(self):
        return list(self._remote), list(self._notes)

    def prepare(self, origin, area, progress=None):
        self.prepared.append((origin.label, area.kind))
        if self._prepare:
            return self._prepare(origin, area, progress)
        return Path(str(area.path))

    def cancel(self):
        self.cancelled = True

    def cleanup(self):
        self.cleaned += 1


def home_snapshot(
    key, root: Path, label="USB", kind="usb", target=Path("/proc/restore-fusion-never-writable")
):
    return Snapshot(
        key,
        datetime.strptime(key[:17], "%Y-%m-%d_%H%M%S"),
        "completed",
        [Origin(label, kind, [Area("home", "Home", root, target)], "rsync")],
    )


def two_area_snapshot(key, home: Path, root: Path, home_target: Path):
    return Snapshot(
        key,
        datetime.strptime(key[:17], "%Y-%m-%d_%H%M%S"),
        "completed",
        [
            Origin(
                "USB",
                "usb",
                [Area("home", "Home", home, home_target), Area("root", "Root /", root, Path("/"))],
                "btrfs",
            )
        ],
    )


def remote_snapshot(
    key,
    remote_path="/r/home/home_x",
    label="Box",
    target=Path("/proc/restore-fusion-never-writable"),
):
    return Snapshot(
        key,
        datetime.strptime(key[:17], "%Y-%m-%d_%H%M%S"),
        "completed",
        [
            Origin(
                label,
                "remote",
                [Area("home", "Home", remote_path, target, Storage.REMOTE, True)],
                "btrfs",
            )
        ],
    )
