"""Directorio de trabajo por corrida, barrido tras un kill.

Todo lo transitorio de una corrida vive bajo UN directorio con el pid en el nombre
(`<local_snapshots_dir>/.backup-tmp/<pid>-<YYYYMMDD_HHMMSS>/`); un kill duro deja a lo sumo ese
directorio, que se barre en la siguiente corrida solo si el pid ya no existe.

Estos borrados son del estado transitorio PROPIO de la herramienta (nombres con regex estricta,
dentro de `.backup-tmp`), no de snapshots ni de datos del usuario: por eso no pasan por Approval.
"""

import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional

_RUN_TMP_RE = re.compile(r"^(\d+)-\d{8}_\d{6}$")
# restos de versiones viejas en /.snapshots
_LEGACY_TMP_RE = re.compile(r"^\.(state|backup)-\d{8}_\d{6}(\.log)?$")
LEGACY_MAX_AGE = 6 * 3600


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Scratch:
    def __init__(
        self, local_snapshots_dir: Path, emit: Optional[Callable[[str, str], None]] = None
    ):
        self.base = Path(local_snapshots_dir)
        self.root = self.base / ".backup-tmp"
        self._emit = emit or (lambda t, m: None)

    def sweep_stale(self) -> List[str]:
        swept: List[str] = []
        try:
            entries = list(self.root.iterdir()) if self.root.is_dir() else []
        except OSError:
            entries = []
        for entry in entries:
            m = _RUN_TMP_RE.match(entry.name)
            if m and not pid_alive(int(m.group(1))):
                shutil.rmtree(entry, ignore_errors=True)
                swept.append(entry.name)
                self._emit("info", f"swept stale scratch dir {entry.name}")
        try:
            legacy = list(self.base.iterdir()) if self.base.is_dir() else []
        except OSError:
            legacy = []
        cutoff = time.time() - LEGACY_MAX_AGE
        for entry in legacy:
            if not _LEGACY_TMP_RE.match(entry.name):
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                swept.append(entry.name)
                self._emit("info", f"swept legacy scratch {entry.name}")
            except OSError:
                pass
        return swept

    def new_run_dir(self, compact: str) -> Path:
        run = self.root / f"{os.getpid()}-{compact}"
        run.mkdir(parents=True, exist_ok=True)
        return run

    def keep_last_log(self, run_dir: Path) -> None:
        """Deja solo la bitácora de la última corrida (un archivo, se sobrescribe)."""
        try:
            log = run_dir / "backup.log"
            if log.is_file():
                shutil.copy2(log, self.base / ".backup-last.log")
        except OSError:
            pass

    def cleanup(self, run_dir: Path) -> None:
        if Path(run_dir).parent == self.root:  # nunca borra fuera de .backup-tmp
            shutil.rmtree(run_dir, ignore_errors=True)
