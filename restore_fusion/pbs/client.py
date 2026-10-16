"""Llamadas a `proxmox-backup-client` (todas argv, nunca shell) y manejo seguro del secreto.

* PBS_PASSWORD viaja SOLO por el entorno del proceso hijo: jamás en argv (se vería en `ps`), jamás
  en un mensaje, log o repr. PBS_REPOSITORY no lleva secreto (usuario!token@host:datastore).
* Una consulta fallida es un error (`PbsError`), nunca "no hay snapshots" (misma regla que el remoto).
"""

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

TOOL = "proxmox-backup-client"
# Proxmox es opcional; no se inventa ningún nombre de paquete por distro.
CLIENT_HINT = (
    "Proxmox Backup Server support is optional. The client comes from the Proxmox "
    "repository; if you do not use PBS you can ignore this."
)
DEFAULT_ARCHIVES = ("root.pxar", "home.pxar")


class PbsError(RuntimeError):
    pass


@dataclass(frozen=True)
class PbsSnapshot:
    backup_id: str
    backup_time: int  # epoch (UTC)
    size: int = 0

    @property
    def path(self) -> str:
        """host/<id>/<YYYY-MM-DDTHH:MM:SSZ>, el formato que esperan `mount` y `snapshot files`."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.backup_time))
        return f"host/{self.backup_id}/{ts}"

    @property
    def local_time(self) -> datetime:
        return datetime.fromtimestamp(self.backup_time)

    @property
    def key(self) -> str:
        return self.local_time.strftime("%Y-%m-%d_%H%M%S")


def pbs_env(cfg, base=None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PBS_PASSWORD"] = cfg.pbs_password
    if cfg.pbs_fingerprint:
        env["PBS_FINGERPRINT"] = cfg.pbs_fingerprint
    return env


def check_configured(cfg) -> Optional[str]:
    """Texto de ayuda si falta configuración; None si está lista. No incluye ningún valor secreto."""
    if cfg.pbs_repository and cfg.pbs_password:
        return None
    return (
        "No PBS target configured. Add to your config.conf:\n\n"
        "  PBS_REPOSITORY=user@realm!token@host:datastore\n"
        "  PBS_PASSWORD=<token secret>\n"
        "  PBS_FINGERPRINT=<server fingerprint, from 'proxmox-backup-manager cert info'>\n"
    )


class PbsClient:
    def __init__(
        self,
        cfg,
        *,
        run: Callable = subprocess.run,
        which: Callable = shutil.which,
        sleep: Callable = time.sleep,
        wait_seconds: float = 5.0,
    ):
        self.cfg, self._run, self._which, self._sleep = cfg, run, which, sleep
        self.wait_seconds = wait_seconds

    def installed(self) -> bool:
        return self._which(TOOL) is not None

    def _call(self, args: List[str]):
        return self._run([TOOL, *args], env=pbs_env(self.cfg), capture_output=True, text=True)

    def list_snapshots(self, hostname: str) -> List[PbsSnapshot]:
        res = self._call(
            ["snapshot", "list", f"host/{hostname}", "--repository", self.cfg.pbs_repository,
             "--output-format", "json"]
        )  # fmt: skip
        if res.returncode != 0:
            raise PbsError((res.stderr or "").strip() or "could not list snapshots")
        try:
            rows = json.loads(res.stdout)
            out = [
                PbsSnapshot(str(r["backup-id"]), int(r["backup-time"]), int(r.get("size") or 0))
                for r in rows
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise PbsError(f"unreadable snapshot list from PBS: {exc}") from exc
        return sorted(out, key=lambda s: s.backup_time, reverse=True)

    def archives(self, snapshot_path: str) -> List[str]:
        """Archivos .pxar del snapshot. Si no se puede preguntar, se prueban los dos nombres usuales
        (el que no exista fallará al montar y se omite)."""
        res = self._call(
            ["snapshot", "files", snapshot_path, "--repository", self.cfg.pbs_repository,
             "--output-format", "json"]
        )  # fmt: skip
        if res.returncode == 0:
            try:
                names = [r["filename"] for r in json.loads(res.stdout)]
                found = [
                    n for n in names if str(n).endswith(".pxar") or str(n).endswith(".pxar.didx")
                ]
                return [n[: -len(".didx")] if n.endswith(".didx") else n for n in found]
            except (ValueError, KeyError, TypeError):
                pass
        return list(DEFAULT_ARCHIVES)

    def mount(self, snapshot_path: str, archive: str, target: Path) -> None:
        """Monta un archivo en `target` (FUSE, solo lectura). Lanza PbsError si no aparece."""
        target.mkdir(parents=True, exist_ok=True)
        res = self._call(["mount", snapshot_path, archive, str(target),
                          "--repository", self.cfg.pbs_repository])  # fmt: skip
        if res.returncode != 0:
            raise PbsError(f"mount {archive}: {(res.stderr or '').strip() or 'failed'}")
        # `mount` se va a segundo plano: se espera a que FUSE enganche (el contenido aparece)
        waited = 0.0
        while waited <= self.wait_seconds:
            try:
                if any(target.iterdir()):
                    return
            except OSError:
                pass
            self._sleep(0.1)
            waited += 0.1
        raise PbsError(f"mount {archive}: nothing appeared at {target}")

    def unmount(self, target: Path) -> None:
        """Desmonta y quita el directorio con `rmdir` (jamás rmtree: podría recorrer un montaje vivo)."""
        tool = self._which("fusermount3") or self._which("fusermount")
        if tool:
            self._run([tool, "-uz", str(target)], capture_output=True)
        elif self._which("umount"):
            self._run(["umount", str(target)], capture_output=True)
        try:
            target.rmdir()
        except OSError:
            pass
