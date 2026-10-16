"""Destino SSH. Todo comando local es una lista argv; el comando REMOTO se arma con
`shlex.join(argv)` (citado estricto), nunca con f-strings.

De restore-tui/destinations.py (RemoteDestination: ruta LAN primero, presupuesto de tamaño)
y btrfs-restore-tui/btrfs_ops.py (build_ssh_args: clave y known_hosts del usuario real).

  * `StrictHostKeyChecking=accept-new` SIEMPRE; nunca "no".
  * Una consulta que falla lanza RemoteQueryError: "no pude preguntar" nunca se lee como
    "está vacío" (ext4 devolvía [] ante un timeout).
  * Borrar exige un Approval que cubra el snapshot.
"""

import json
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..model.manifest import is_valid_snapshot_name
from ..safety.approval import Approval
from .base import SUFFIXES, Destination, RemoteQueryError

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=15",
]  # fmt: skip

SSH_FAILED = 255  # ssh sale con 255 cuando falla la conexión (no el comando remoto)


def build_ssh_argv(
    user_host: str,
    port: int = 22,
    *,
    identity: Optional[Path] = None,
    known_hosts: Optional[Path] = None,
) -> List[str]:
    """`ssh` + opciones + destino, como lista. Compartido con los motores y el restore."""
    argv = ["ssh", *SSH_OPTS, "-p", str(int(port))]
    if identity is not None and Path(identity).exists():
        argv += ["-i", str(identity)]
    if known_hosts is not None and Path(known_hosts).exists():
        argv += ["-o", f"UserKnownHostsFile={known_hosts}"]
    argv.append(user_host)
    return argv


def user_ssh_files(home: Path) -> Tuple[Path, Path]:
    return Path(home) / ".ssh" / "id_ed25519", Path(home) / ".ssh" / "known_hosts"


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# Una sola llamada, solo lectura, todo argv (la ruta va como argumento, nunca interpolada). Si la
# carpeta aún no existe (primer backup) se sube al ancestro que sí existe: es el mismo disco.
_FS_PROBE = (
    'p="$1"; while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done; '
    'stat -f -c %T "$p"'
)
_FS_NAMES = {"ext2/ext3": "ext4 (or ext2/ext3)"}  # GNU stat no distingue la familia ext


def remote_fs_type(dest: "SSHDestination", ssh_capture: Optional[Callable] = None) -> Optional[str]:
    """Sistema de archivos de la ruta remota (p. ej. 'btrfs'), o None si no se pudo determinar
    (sin red, sin `stat`, salida rara). Solo lectura; nunca lanza. `ssh_capture(ssh_argv, remote,
    cmd_argv, timeout)` es inyectable (BtrfsOps.ssh_capture tiene esa firma)."""
    if not dest.root:
        return None
    argv = dest.ssh_argv()
    cmd = ["sh", "-c", _FS_PROBE, "sh", dest.root]
    try:
        if ssh_capture is not None:
            res = ssh_capture(argv[:-1], argv[-1], cmd, 25)
        else:
            res = dest.run_cmd(cmd, timeout=25)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    fs = (res.stdout or "").strip().splitlines()
    fs = fs[-1].strip().lower() if fs else ""
    if not fs or " " in fs or fs.startswith("unknown"):
        return None
    return _FS_NAMES.get(fs, fs)


def remote_fs_message(label: str, root: str, fs: Optional[str]) -> str:
    """Texto para el usuario según `remote_fs_type`. '' = btrfs, no hay nada que decir. Nunca
    bloquea ni cambia el motor: solo informa."""
    if fs is None:
        return f"{label}: could not determine the remote filesystem; continuing."
    if fs == "btrfs":
        return ""
    return (
        f"{label}: the remote path {root} is {fs}, not btrfs. The btrfs engine "
        "needs btrfs on the server (or a btrfs image mounted there), or set ENGINE=rsync "
        "for plain copies. The send is likely to fail at 'btrfs receive'."
    )


class SSHDestination(Destination):
    def __init__(
        self,
        *,
        host: str,
        root: str,
        user: str,
        host_lan: str = "",
        port: int = 22,
        label: str = "",
        remote_max_gb: int = 0,
        home: Optional[Path] = None,
        tcp_check: Callable[[str, int], bool] = tcp_open,
    ):
        self.user = user
        self.port = int(port or 22)
        self.root = root.rstrip("/")
        self.snaps = f"{self.root}/snapshots"
        self.latest_path = f"{self.root}/latest"
        self.label = label or host
        self.remote_max_gb = remote_max_gb
        self._tcp_check = tcp_check
        self._identity, self._known_hosts = user_ssh_files(home) if home else (None, None)
        self._wan = (host or "").strip()
        self._lan = (host_lan or "").strip()
        self._candidates = [h for h in dict.fromkeys((self._lan, self._wan)) if h]
        self.host = self._candidates[0] if self._candidates else self._wan
        self._resolved = False

    # -- ruta (LAN primero, luego WAN) --------------------------------------------
    def _resolve_host(self) -> str:
        if self._resolved:
            return self.host
        for h in self._candidates:
            if self._tcp_check(h, self.port):
                self.host = h
                break
        else:
            if self._candidates:
                self.host = self._candidates[-1]
        self._resolved = True
        return self.host

    def route_kind(self) -> str:
        return "LAN" if (self._lan and self.host == self._lan) else "WAN"

    def force_route(self, kind: str) -> None:
        """'auto' (LAN y luego WAN), 'lan' o 'wan'."""
        if kind == "lan" and self._lan:
            self._candidates = [self._lan]
        elif kind == "wan" and self._wan:
            self._candidates = [self._wan]
        else:
            self._candidates = [h for h in dict.fromkeys((self._lan, self._wan)) if h]
        self._resolved = False
        if self._candidates:
            self.host = self._candidates[0]

    # -- ssh ------------------------------------------------------------------------------
    def ssh_argv(self) -> List[str]:
        self._resolve_host()
        return build_ssh_argv(
            f"{self.user}@{self.host}",
            self.port,
            identity=self._identity,
            known_hosts=self._known_hosts,
        )

    def run_cmd(self, argv: Sequence[str], timeout: int = 60, stdin: Optional[str] = None):
        remote = shlex.join([str(a) for a in argv])
        return subprocess.run(
            [*self.ssh_argv(), remote],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin,
        )

    def _ask(self, argv: Sequence[str], what: str, timeout: int = 60, stdin: Optional[str] = None):
        """run_cmd que convierte 'no pude preguntar' en RemoteQueryError."""
        try:
            res = self.run_cmd(argv, timeout=timeout, stdin=stdin)
        except subprocess.TimeoutExpired as exc:
            raise RemoteQueryError(f"{what}: timed out after {exc.timeout}s") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteQueryError(f"{what}: {exc}") from exc
        if res.returncode == SSH_FAILED:
            raise RemoteQueryError(f"{what}: ssh failed: {(res.stderr or '').strip()[:160]}")
        return res

    def rsync_transport(self) -> List[str]:
        """['-e', 'ssh ...'] para rsync (rsync exige una sola cadena)."""
        return ["-e", shlex.join(self.ssh_argv()[:-1])]

    def tree_path(self, snap: str) -> str:
        self._need_name(snap)
        return f"{self.snaps}/{snap}"

    def spec(self, path: str) -> str:
        self._resolve_host()
        return f"{self.user}@{self.host}:{path}"

    # -- disponibilidad -----------------------------------------------------------------------
    def reachable(self) -> Tuple[bool, str]:
        if not (self.root and self._candidates):
            return False, "remote target not configured"
        try:
            res = self.run_cmd(["true"], timeout=20)
        except subprocess.TimeoutExpired:
            return False, f"{self.host} did not answer (timeout)"
        except OSError as exc:
            return False, f"ssh failed: {exc}"
        if res.returncode != 0:
            return (
                False,
                f"ssh to {self.user}@{self.host} failed: {(res.stderr or '').strip()[:120]}",
            )
        return True, f"{self.user}@{self.host}:{self.port}  [{self.route_kind()}]"

    def ensure_ready(self) -> Tuple[bool, str]:
        try:
            r = self.run_cmd(["mkdir", "-p", self.snaps], timeout=30)
            if r.returncode == 0:
                r = self.run_cmd(["test", "-w", self.snaps], timeout=30)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"ssh error: {exc}"
        if r.returncode == 0:
            return True, "writable"
        return False, ((r.stderr or "").strip() or "remote path not writable")[:160]

    def display_root(self) -> str:
        return f"{self.user}@{self.host}:{self.snaps}"

    # -- snapshots ---------------------------------------------------------------------------------
    def list_snapshots(self) -> List[str]:
        exists = self._ask(["test", "-d", self.snaps], "could not check the remote snapshots dir")
        if exists.returncode == 1:
            return []  # el remoto respondió: la carpeta no existe todavía
        res = self._ask(["ls", "-1", "--", self.snaps], "could not list remote snapshots")
        if res.returncode != 0:
            raise RemoteQueryError(
                f"could not list remote snapshots (exit {res.returncode}): "
                f"{(res.stderr or '').strip()[:120]}"
            )
        return sorted(n for n in res.stdout.split() if is_valid_snapshot_name(n))

    def list_entries(self, snap: str) -> List[str]:
        self._need_name(snap)
        res = self._ask(["ls", "-1", "--", f"{self.snaps}/{snap}"], "could not list snapshot")
        return sorted(res.stdout.split()) if res.returncode == 0 else []

    def read_manifest(self, snap: str) -> dict:
        self._need_name(snap)
        res = self._ask(["cat", f"{self.snaps}/{snap}/manifest.json"], "could not read manifest")
        if res.returncode != 0:
            return {}  # el remoto respondió: no hay manifest
        try:
            data = json.loads(res.stdout)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_manifest(self, snap: str, data: dict) -> None:
        self._need_name(snap)
        path = f"{self.snaps}/{snap}/manifest.json"
        res = self.run_cmd(
            ["sh", "-c", 'cat > "$1"', "sh", path], timeout=30, stdin=json.dumps(data, indent=2)
        )
        if res.returncode != 0:
            raise OSError(f"remote manifest write failed: {(res.stderr or '').strip()}")

    def reserve_snapshot(self, name: str) -> str:
        res = self.run_cmd(["mkdir", "-p", self.snaps], timeout=30)
        if res.returncode != 0:
            raise OSError(f"cannot create remote snapshots dir: {(res.stderr or '').strip()[:120]}")
        for suffix in SUFFIXES:
            cand = f"{name}{suffix}"
            r = self.run_cmd(["mkdir", f"{self.snaps}/{cand}"], timeout=30)
            if r.returncode == 0:
                return cand
        raise OSError(f"too many remote snapshots for {name}")

    def push_meta(self, stage_dir: Path, snap: str) -> None:
        self._need_name(snap)
        cmd = [
            "rsync", "-a", "--numeric-ids",
            *self.rsync_transport(),
            f"{stage_dir}/",
            self.spec(f"{self.snaps}/{snap}/"),
        ]  # fmt: skip
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise OSError(
                f"remote meta push failed ({r.returncode}): {(r.stderr or '').strip()[:200]}"
            )
        self.run_cmd(["chmod", "755", f"{self.snaps}/{snap}/restore.sh"], timeout=20)

    def update_latest(self, snap: str) -> None:
        self._check_latest(snap)
        r = self.run_cmd(["ln", "-sfn", f"snapshots/{snap}", self.latest_path], timeout=20)
        if r.returncode != 0:
            raise OSError(f"could not update 'latest': {(r.stderr or '').strip()[:120]}")

    def remove(self, snap: str, approval: Optional[Approval]) -> None:
        self._check_removal(snap, approval)
        r = self.run_cmd(["rm", "-rf", "--", f"{self.snaps}/{snap}"], timeout=120)
        if r.returncode != 0:
            raise OSError((r.stderr or "").strip())

    def write_kit_files(self, files: Dict[str, bytes]) -> None:
        import shutil
        import tempfile

        stage = Path(tempfile.mkdtemp(prefix="restore-fusion-kit-"))
        try:
            for rel, content in files.items():
                p = stage / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                if rel.endswith(".sh"):
                    p.chmod(0o755)
            cmd = [
                "rsync", "-a", "--numeric-ids",
                *self.rsync_transport(),
                f"{stage}/",
                self.spec(f"{self.root}/"),
            ]  # fmt: skip
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise OSError(f"kit push failed ({r.returncode}): {(r.stderr or '').strip()[:200]}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    # -- retención ----------------------------------------------------------------------------------------
    def _tree_bytes(self) -> Optional[int]:
        try:
            r = self.run_cmd(["du", "-sb", "--", self.root], timeout=120)
            return int(r.stdout.split()[0])
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
            return None

    def usage_summary(self) -> str:
        b = self._tree_bytes()
        if b is None:
            return "remote tree size unknown"
        cap = f" / {self.remote_max_gb} GB budget" if self.remote_max_gb else ""
        return f"snapshot tree {b / 1e9:.1f} GB{cap}"

    def should_prune_more(self, completed_count: int, min_keep: int) -> bool:
        if not self.remote_max_gb or completed_count <= max(1, min_keep):
            return False
        b = self._tree_bytes()
        return b is not None and b > self.remote_max_gb * 1_000_000_000

    @staticmethod
    def _need_name(snap: str) -> None:
        if not is_valid_snapshot_name(snap):
            raise ValueError(f"invalid snapshot name: {snap!r}")
