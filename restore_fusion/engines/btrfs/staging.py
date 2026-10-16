"""Staging btrfs: recibir un subvolumen remoto o un stream `.btrfs.zst` en un subvolumen temporal para
poder navegarlo ("perdí todo mi home"). Es el plan B del montaje sshfs. De btrfs-restore-tui/engine.py.

  * Todo es una lista de etapas argv; el comando remoto se cita con shlex.join.
  * Cada corrida usa `<staging>/<pid>-<slug>/`. La limpieza solo toca entradas con ese patrón
    (estado transitorio PROPIO, como backup/scratch.py): jamás otra cosa que esté en esa carpeta.
  * Necesita root y un btrfs de destino; si no hay uno se dice claro (STAGING_DIR).
"""

import atexit
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

from ...config.schema import Config
from ...destinations.ssh import build_ssh_argv, user_ssh_files
from ...drives import fstype_of
from ...model.snapshots import Area, Storage

logger = logging.getLogger("restore_fusion")

DEFAULT_STAGING = Path("/.snapshots/staging")
_RUN_RE = re.compile(r"^(\d+)-")
# pv entre el productor y `btrfs receive`: contador de bytes/tasa en vivo
PV_ARGS = ["pv", "-f", "-b", "-t", "-r", "-i", "1"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Staging:
    def __init__(
        self,
        cfg: Config,
        progress_cb: Optional[Callable[[str], None]] = None,
        *,
        fstype: Callable[[Path], Optional[str]] = fstype_of,
        which: Callable = shutil.which,
    ):
        self.cfg = cfg
        self.dir = Path(cfg.staging_dir) if cfg.staging_dir else DEFAULT_STAGING
        self._progress_cb = progress_cb
        self._fstype, self._which = fstype, which
        self._active: List[subprocess.Popen] = []
        self._run_dir: Optional[Path] = None

    # -- destino de staging ----------------------------------------------------------------
    def resolve_dir(self) -> Path:
        """STAGING_DIR, luego /.snapshots/staging, luego un directorio en la raíz de /: el primero que
        esté en btrfs. Error claro si ninguno lo está."""
        cands: List[Path] = []
        if self.cfg.staging_dir:
            cands.append(Path(self.cfg.staging_dir))
        cands += [DEFAULT_STAGING, Path("/.restore-fusion-staging")]
        for cand in cands:
            base = cand
            while not base.exists() and base != base.parent:
                base = base.parent
            if self._fstype(base) == "btrfs":
                return cand
        raise RuntimeError(
            "No btrfs filesystem found for restore staging. Set STAGING_DIR in "
            "~/.config/restore-tui/config.conf to a path on a btrfs mount (the snapshot stream is "
            "received there before you browse it)."
        )

    # -- etapas del pipeline ------------------------------------------------------------------
    def stages(self, area: Area, run_dir: Path, user: str) -> Optional[List[List[str]]]:
        """Pipeline argv para dejar `area` navegable, o None si no necesita staging."""
        receive = ["btrfs", "receive", str(run_dir)]
        if area.storage is Storage.REMOTE:
            if not (self.cfg.remote_host and self.cfg.remote_path):
                raise RuntimeError("Remote host is not configured (REMOTE_HOST / REMOTE_PATH)")
            ident, known = user_ssh_files(self.cfg.home)
            ssh = build_ssh_argv(
                f"{self.cfg.remote_user or user}@{self.cfg.remote_host}",
                self.cfg.remote_port, identity=ident, known_hosts=known,
            )  # fmt: skip
            src = str(area.path)
            send = shlex.join(["sudo", "btrfs", "send", src])
            return [[*ssh, send], receive]
        if area.storage is Storage.STREAM:
            return [["zstd", "-dc", str(area.path)], receive]
        return None

    # -- ejecutar -------------------------------------------------------------------------------
    def _maybe_pv(self, stages: List[List[str]]):
        if self._progress_cb and self._which("pv") and len(stages) >= 2:
            spliced = [*stages[:-1], list(PV_ARGS), stages[-1]]
            return spliced, len(spliced) - 2
        return list(stages), -1

    def _pump_pv(self, proc: subprocess.Popen) -> None:
        buf = b""
        try:
            while True:
                chunk = proc.stderr.read(64)
                if not chunk:
                    break
                buf += chunk
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if text and self._progress_cb:
                        self._progress_cb(text)
        except (OSError, ValueError):
            pass

    def run_pipeline(self, stages: List[List[str]], *, progress: bool = False) -> None:
        """`stages[0] | stages[1] | ...` con listas argv (nunca un shell). Cada etapa en su propia sesión
        para que cancelar mate todo el árbol (incluido un ssh remoto). RuntimeError con el stderr si falla.
        """
        pv_index = -1
        if progress:
            stages, pv_index = self._maybe_pv(stages)
        procs: List[subprocess.Popen] = []
        prev = None
        try:
            for stage in stages:
                p = subprocess.Popen(stage, stdin=prev, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)  # fmt: skip
                procs.append(p)
                if prev is not None:
                    prev.close()
                prev = p.stdout
        except FileNotFoundError as exc:
            for p in procs:
                p.kill()
            raise RuntimeError(f"missing command: {exc}") from exc
        self._active = procs
        pv_proc = procs[pv_index] if pv_index >= 0 else None
        pump = None
        if pv_proc is not None:
            pump = threading.Thread(target=self._pump_pv, args=(pv_proc,), daemon=True)
            pump.start()
        errs: List[str] = []
        try:
            for p in procs:
                if p is pv_proc:
                    p.wait()
                    continue
                _, err = p.communicate()
                if err:
                    errs.append(err.decode("utf-8", "replace").strip())
        except BaseException:
            self.cancel()
            if pump is not None:
                pump.join(timeout=1.0)
            raise
        if pump is not None:
            pump.join(timeout=1.0)
        self._active = []
        failed = next(
            (p for p in procs if p is not pv_proc and p.returncode not in (0, None)), None
        )
        if failed is not None:
            msg = " | ".join(e for e in errs if e) or "stream failed"
            low = msg.lower()
            if "parent" in low or "cannot find" in low or "no such file" in low:
                raise RuntimeError(
                    "Cannot receive incremental delta: parent subvolume missing on local disk. "
                    "This backup requires its base parent."
                )
            raise RuntimeError(msg)

    def cancel(self) -> None:
        """SIGTERM al grupo de cada etapa y SIGKILL a lo que siga vivo; limpia el staging propio."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            alive = [p for p in self._active if p.poll() is None]
            if not alive:
                break
            for proc in alive:
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.send_signal(sig)
                    except OSError:
                        pass
            for proc in alive:
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
        self._active = []
        self.cleanup_own()

    def deploy(self, area: Area, user: str) -> Path:
        """Recibe `area` en `<staging>/<pid>-<slug>/` y devuelve la carpeta navegable (entrando a la
        carpeta del usuario si el área es un home)."""
        self.dir = self.resolve_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.sweep_orphans()
        self.cleanup_own()
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{area.kind}_{Path(str(area.path)).name}")[:48]
        run_dir = self.dir / f"{os.getpid()}-{slug}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir
        atexit.register(self.cleanup_own)
        stages = self.stages(area, run_dir, user)
        if stages is None:
            self.cleanup_own()
            return Path(str(area.path))
        self.run_pipeline(stages, progress=True)
        for item in run_dir.iterdir():
            if item.is_dir():
                user_dir = item / user
                return user_dir if (area.home_user_subdir and user_dir.exists()) else item
        raise RuntimeError(f"No received subvolume found in {run_dir}")

    # -- limpieza (solo entradas `<pid>-...` dentro de la carpeta de staging) -----------------------------
    def _purge(self, p: Path) -> None:
        """Borra el subvolumen btrfs en `p`; si es un directorio normal (una corrida `<pid>-<slug>/` que
        contiene el subvolumen recibido) baja a sus hijos y quita el directorio."""
        try:
            r = subprocess.run(
                ["btrfs", "subvolume", "delete", str(p)], capture_output=True, timeout=15
            )
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("could not delete staging subvolume %s: %s", p, exc)
        if p.is_dir() and not p.is_symlink():
            for child in list(p.iterdir()):
                self._purge(child)
            try:
                p.rmdir()
            except OSError:
                shutil.rmtree(p, ignore_errors=True)

    def sweep_orphans(self) -> None:
        """Quita el staging de corridas muertas (pid ya no existe); una corrida viva no se toca."""
        if not self.dir.exists():
            return
        for entry in self.dir.iterdir():
            m = _RUN_RE.match(entry.name)
            if m and not _pid_alive(int(m.group(1))):
                self._purge(entry)

    def cleanup_all(self) -> None:
        """Todo el staging con nombre `<pid>-...` (restore-tui --gc / uninstall). Lo demás no se toca."""
        if not self.dir.exists():
            return
        for entry in self.dir.iterdir():
            if _RUN_RE.match(entry.name):
                self._purge(entry)

    def cleanup_own(self) -> None:
        run = self._run_dir
        if run is not None and run.exists() and run.parent == self.dir:
            self._purge(run)
        self._run_dir = None
