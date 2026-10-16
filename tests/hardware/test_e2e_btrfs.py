"""Prueba de punta a punta del backend btrfs REAL (BtrfsOps) sobre un filesystem en un
dispositivo loop: snapshot -> send -> receive, luego un `send -p` incremental.

Portado de btrfs-restore-tui/tests/test_e2e_btrfs.py. Opt-in: necesita root, `mkfs.btrfs`,
soporte btrfs en el kernel y RESTORE_FUSION_E2E=1. En cualquier otro caso se SALTA (SKIPPED);
nunca cuenta como pasada. needs real-hardware test.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from restore_fusion.engines.btrfs.ops import BtrfsOps

pytestmark = pytest.mark.needs_root_btrfs


@pytest.fixture
def mnt(tmp_path):
    img, mount = tmp_path / "fs.img", tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(["truncate", "-s", "512M", str(img)], check=True)
    subprocess.run(["mkfs.btrfs", "-q", str(img)], check=True)
    subprocess.run(["mount", "-o", "loop", str(img), str(mount)], check=True)
    yield mount
    subprocess.run(["umount", str(mount)], capture_output=True)


def _subvol(mnt: Path, name: str) -> Path:
    p = mnt / name
    subprocess.run(["btrfs", "subvolume", "create", str(p)], check=True, capture_output=True)
    return p


def test_full_then_incremental_send_receive(mnt):
    ops = BtrfsOps()
    src = _subvol(mnt, "src")
    (src / "keep.txt").write_text("hello\n")
    (src / "sub").mkdir()
    (src / "sub" / "nested.txt").write_text("one\n")

    snap1 = mnt / "snap1"
    ops.snapshot_ro(src, snap1)
    recv1 = mnt / "recv1"
    recv1.mkdir()
    assert ops.send_local_receive(snap1, None, recv1) == 0
    assert (recv1 / "snap1" / "keep.txt").read_text() == "hello\n"
    assert (recv1 / "snap1" / "sub" / "nested.txt").read_text() == "one\n"

    (src / "added.txt").write_text("brand new\n")
    (src / "keep.txt").write_text("hello again\n")
    snap2 = mnt / "snap2"
    ops.snapshot_ro(src, snap2)
    recv2 = mnt / "recv2"
    recv2.mkdir()
    ops.send_local_receive(snap1, None, recv2)  # el padre debe existir del lado receptor
    assert ops.send_local_receive(snap2, snap1, recv2) == 0
    assert (recv2 / "snap2" / "keep.txt").read_text() == "hello again\n"
    assert (recv2 / "snap2" / "added.txt").read_text() == "brand new\n"
    for sv in ("snap1", "snap2", "src"):
        subprocess.run(["btrfs", "subvolume", "delete", str(mnt / sv)], capture_output=True)
