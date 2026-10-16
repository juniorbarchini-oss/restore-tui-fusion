"""FakeBtrfsOps: reemplaza la capa de comandos btrfs/zstd/ssh. Los subvolúmenes son directorios
normales, "send" copia árboles, el uso de disco y los puntos de falla son configurables.
(Portado de btrfs-restore-tui/tests/test_backup.py.)"""

import shutil
import subprocess
from pathlib import Path

from restore_fusion.engines.btrfs.ops import prune_paths


class CP:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeBtrfsOps:
    def __init__(
        self,
        *,
        target_is_btrfs=True,
        disk_percent=10.0,
        fail_send_on=None,
        list_failure=None,
        missing_dir=False,
        interrupt_send=False,
        remote_fs="btrfs",
    ):
        self.remote_fs = remote_fs  # str | None (stat sin salida) | "ssh255" | "timeout"
        self.missing_dir = missing_dir
        self.list_failure = list_failure  # None | "timeout" | "ssh255"
        self.target_is_btrfs = target_is_btrfs
        self.disk_percent = disk_percent
        self.fail_send_on = fail_send_on or set()
        self.interrupt_send = interrupt_send
        self.subvolumes = set()
        self.sent = []
        self.ssh_sent = []
        self.remote_subvols = {"root": [], "home": []}
        self.remote_partial = {}
        self.remote_children = set()
        self.receive_alive = False
        self.ssh_deleted = []
        self.ssh_calls = []
        self.pushed_trees = []
        self.last_stderr = ""
        self.deleted_local = []

    def snapshot_ro(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, dirs_exist_ok=True)
        (dest / ".subvol_marker").write_text(str(source))
        self.subvolumes.add(str(dest))

    def snapshot_rw(self, source: Path, dest: Path) -> None:
        self.snapshot_ro(source, dest)

    def set_readonly(self, path: Path, value: bool = True) -> None:
        pass

    def prune_paths(self, root: Path, patterns) -> list:
        return prune_paths(Path(root), patterns, self.is_subvolume)

    def delete_subvolume(self, path: Path) -> None:
        self.deleted_local.append(Path(path).name)
        self.subvolumes.discard(str(path))
        shutil.rmtree(path, ignore_errors=True)

    def is_subvolume(self, path: Path) -> bool:
        return str(path) in self.subvolumes

    def is_btrfs(self, path: Path) -> bool:
        return self.target_is_btrfs

    def send_local_receive(self, source, parent, dest_dir) -> int:
        kind = source.name.split("_", 1)[0]
        self.sent.append((kind, parent.name if parent else None))
        if self.interrupt_send:
            raise KeyboardInterrupt
        if kind in self.fail_send_on:
            return 1
        received = dest_dir / source.name
        shutil.copytree(source, received)
        self.subvolumes.add(str(received))
        return 0

    def send_to_stream(self, source, parent, out_file, level=3) -> int:
        kind = source.name.split("_", 1)[0]
        self.sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        out_file.write_bytes(b"FAKE-BTRFS-STREAM")
        return 0

    def disk_usage_percent(self, path: Path) -> float:
        return self.disk_percent

    # -- remoto ------------------------------------------------------
    def ssh_capture(self, ssh_argv, remote, cmd_argv, timeout=25):
        self.ssh_calls.append(list(cmd_argv))
        out = ""
        if cmd_argv[0] == "sh" and "stat -f" in cmd_argv[2]:
            if self.remote_fs == "timeout":
                raise subprocess.TimeoutExpired(cmd_argv, timeout)
            if self.remote_fs == "ssh255":
                return CP(255, "", "ssh: connect to host: No route to host")
            return CP(0 if self.remote_fs else 1, (self.remote_fs or "") + "\n")
        if cmd_argv[0] == "pgrep":
            return CP(0 if self.receive_alive else 1)
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "delete"]:
            self.ssh_deleted.append(cmd_argv[-1])
            return CP(0)
        if cmd_argv[:2] == ["test", "-d"] and self.missing_dir:
            return CP(1)
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            if self.list_failure == "timeout":
                raise subprocess.TimeoutExpired(cmd_argv, timeout)
            if self.list_failure == "ssh255":
                return CP(255, "", "ssh: connect to host: No route to host")
            kind = cmd_argv[-1].rstrip("/").split("/")[-1]
            out = "".join(
                f"ID 1 gen 1 top level 5 parent_uuid - received_uuid u-{n} uuid id-{n} path {kind}/{n}\n"
                for n in self.remote_subvols.get(kind, [])
            )
            if "-r" not in cmd_argv:  # -r = solo lectura: sin -r aparecen las copias a medias
                for n in self.remote_partial.get(kind, []):
                    out += (
                        f"ID 2 gen 2 top level 5 parent_uuid - received_uuid - "
                        f"uuid id-{n} path {kind}/{n}\n"
                    )
                for n in self.remote_partial.get(kind, []):
                    if n in self.remote_children:
                        out += (
                            f"ID 3 gen 3 top level 5 parent_uuid id-{n} received_uuid u-kid "
                            f"uuid id-kid path {kind}/{kind}_20991231_000000\n"
                        )
        return CP(0, out)

    def send_ssh_receive(self, source, parent, ssh_argv, remote, receive_argv):
        kind = source.name.split("_", 1)[0]
        self.ssh_sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        self.remote_subvols.setdefault(kind, []).append(source.name)
        return 0

    def push_tree(self, local_dir, ssh_argv, remote, remote_dir, timeout=120):
        pushed = {}
        for p in Path(local_dir).rglob("*"):
            if p.is_file():
                try:
                    pushed[str(p.relative_to(local_dir))] = p.read_bytes()
                except OSError:
                    pass
        self.pushed_trees.append((remote_dir, pushed))
        return 0, ""
