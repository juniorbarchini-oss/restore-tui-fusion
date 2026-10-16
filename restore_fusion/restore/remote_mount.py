"""Montajes SSHFS de solo lectura del host remoto, para NAVEGAR y traer solo lo marcado (sin
descargar un subvolumen de 30 GB). Une btrfs `engine.py` (mount_remote_snapshot) y restore-tui
`remote_mount.py`. Siempre argv, `ro`, `StrictHostKeyChecking=accept-new`.

Seguridad: cada montaje vive en `<base>/<pid>-<slug>/`; al limpiar se DESMONTA y se usa `rmdir`
(jamás `rmtree`: nunca se recorre algo que pueda seguir montado). Los montajes de corridas muertas se
barren al arrancar; los de una corrida viva (otra restauración en paralelo) no se tocan.
"""

import atexit
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from ..destinations.ssh import SSH_OPTS, user_ssh_files

logger = logging.getLogger("restore_fusion")

_RUN_RE = re.compile(r"^(\d+)-")


def default_base() -> Path:
    return Path(tempfile.gettempdir()) / f"restore-fusion-mnt-{os.getuid()}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sshfs_argv(user_host: str, remote_path: str, mountpoint: Path, port: int = 22,
               home: Optional[Path] = None) -> List[str]:  # fmt: skip
    """`sshfs user@host:ruta punto -o ro,...`; la ruta remota es UN elemento, nunca entra a un shell."""
    argv = ["sshfs", f"{user_host}:{remote_path}", str(mountpoint), "-o", "ro", "-o", "reconnect"]
    for opt in SSH_OPTS[
        1::2
    ]:  # "BatchMode=yes", "ConnectTimeout=10", "StrictHostKeyChecking=accept-new"...
        argv += ["-o", opt]
    argv += ["-o", "compression=yes"]
    if port and int(port) != 22:
        argv += ["-p", str(int(port))]
    if home is not None:
        ident, known = user_ssh_files(home)
        if ident.exists():
            argv += ["-o", f"IdentityFile={ident}"]
        if known.exists():
            argv += ["-o", f"UserKnownHostsFile={known}"]
    return argv


class RemoteMounter:
    def __init__(
        self,
        base: Optional[Path] = None,
        *,
        run: Callable = subprocess.run,
        which: Callable = shutil.which,
        is_mount: Callable[[Path], bool] = lambda p: os.path.ismount(str(p)),
    ):
        self.base = Path(base) if base else default_base()
        self._run, self._which, self._is_mount = run, which, is_mount
        self._mounts: List[Path] = []
        self._registered = False

    # -- montar ----------------------------------------------------------------------
    def mount(self, *, user: str, host: str, remote_path: str, port: int = 22, slug: str = "remote",
              home: Optional[Path] = None, timeout: int = 30) -> Optional[Path]:  # fmt: skip
        """Monta `remote_path` en solo lectura y devuelve el punto de montaje; None si no se pudo
        (sin sshfs, host caído...): el llamador cae a staging o avisa."""
        if not self._which("sshfs"):
            return None
        self.sweep_orphans()
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            os.chmod(self.base, 0o755)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", slug)[:48] or "remote"
            mnt = self.base / f"{os.getpid()}-{safe}"
            mnt.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("cannot prepare the mount point: %s", exc)
            return None
        argv = sshfs_argv(f"{user}@{host}", remote_path, mnt, port, home)
        try:
            r = self._run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("sshfs mount failed: %s", exc)
            self._rmdir(mnt)
            return None
        if r.returncode != 0 or not self._is_mount(mnt):
            logger.warning(
                "sshfs mount of %s failed: %s",
                remote_path,
                (r.stderr or "").strip() or f"exit {r.returncode}",
            )
            self._unmount(mnt)
            self._rmdir(mnt)
            return None
        self._mounts.append(mnt)
        if not self._registered:
            atexit.register(self.cleanup)
            self._registered = True
        return mnt

    # -- limpiar ---------------------------------------------------------------------------
    def _unmount(self, mnt: Path, lazy: bool = False) -> None:
        flag = "-uz" if lazy else "-u"
        for cmd in (["fusermount3", flag, str(mnt)], ["fusermount", flag, str(mnt)],
                    ["umount", *(["-l"] if lazy else []), str(mnt)]):  # fmt: skip
            try:
                if self._run(cmd, capture_output=True, timeout=15).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue

    @staticmethod
    def _rmdir(p: Path) -> None:
        try:
            p.rmdir()  # rmdir, nunca rmtree: si siguiera montado esto falla en vez de recorrerlo
        except OSError:
            pass

    def cleanup(self) -> None:
        for mnt in list(self._mounts):
            self._unmount(mnt)
            self._rmdir(mnt)
        self._mounts = []
        self._rmdir(self.base)

    def sweep_orphans(self) -> None:
        """Desmonta (perezoso) y quita los montajes de corridas muertas; deja los de pids vivos."""
        if not self.base.is_dir():
            return
        for entry in self.base.iterdir():
            m = _RUN_RE.match(entry.name)
            if m and _pid_alive(int(m.group(1))):
                continue
            if not m:
                continue  # no es nuestro: no se toca
            self._unmount(entry, lazy=True)
            self._rmdir(entry)

    @property
    def mounts(self) -> List[Path]:
        return list(self._mounts)
