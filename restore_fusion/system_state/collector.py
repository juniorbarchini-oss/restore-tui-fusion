"""Captura lo que vive FUERA de $HOME y hace falta para reconstruir la máquina desde cero, y
escribe `restore.sh`. Mejor esfuerzo: un paso que falla es un aviso, nunca una excepción.

Lo común vive aquí (os_info, /etc, /usr/local/bin, keyd, crontab); lo propio de cada distro lo
aporta el PackageManager. Siempre escribe os_info.json y un restore.sh ejecutable y válido.
De los dos system_state.py originales (btrfs-restore-tui y restore-tui).
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from ..packages.base import CaptureContext, PackageManager, Roots

MAX_BIN_BYTES = 15 * 1024 * 1024


class SystemStateCollector:
    def __init__(
        self,
        snapshot_dir: Path,
        pm: PackageManager,
        engine: str,
        *,
        roots: Optional[Roots] = None,
        run: Optional[Callable] = None,
        which: Callable = shutil.which,
    ):
        self.snapshot_dir = Path(snapshot_dir)
        self.meta_dir = self.snapshot_dir / "_system_state"
        self.pm, self.engine = pm, engine
        self.warnings: List[str] = []
        self.ctx = CaptureContext(self.meta_dir, self.warnings, roots or Roots(), run, which)

    # -- público -----------------------------------------------------------
    def collect_all(self) -> List[str]:
        self.warnings.clear()
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        steps = [("os_info", self._export_os_info)]
        steps += [(n, (lambda f=f: f(self.ctx))) for n, f in self.pm.capture_steps()]
        steps += [
            ("etc_configs", self._export_etc_configs),
            ("usr_local_bin", self._export_usr_local_bin),
            ("keyd", self._export_keyd),
            ("crontab", self._export_crontab),
            ("restore_script", self._generate_restore_script),
        ]
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - mejor esfuerzo por diseño
                self.warnings.append(f"{name}: {exc}")
        return self.warnings

    # -- pasos comunes ------------------------------------------------------------
    def _export_os_info(self) -> None:
        u = os.uname()
        info = {
            "hostname": u.nodename,
            "arch": u.machine,
            "kernel": u.release,
            "user": os.getenv("SUDO_USER") or os.getenv("USER", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
            "session": os.environ.get("XDG_SESSION_TYPE", ""),
            "captured_at": datetime.now().isoformat(),
            "distro": "Linux",
            "version": "",
            "codename": "",
        }
        osr = self.ctx.roots.etc / "os-release"
        if osr.is_file():
            for line in osr.read_text().splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"')
                if k == "NAME":
                    info["distro"] = v
                elif k == "VERSION_ID":
                    info["version"] = v
                elif k == "VERSION_CODENAME":
                    info["codename"] = v
                elif k == "BUILD_ID":
                    info["build_id"] = v
        info["package_manager"], info["engine"] = self.pm.id, self.engine
        (self.meta_dir / "os_info.json").write_text(json.dumps(info, indent=2))

    def _export_etc_configs(self) -> None:
        target = self.meta_dir / "etc"
        target.mkdir(parents=True, exist_ok=True)
        for rel in self.pm.etc_files():
            p = self.ctx.roots.etc / rel
            if p.is_file():
                try:
                    shutil.copy2(p, target / p.name)
                except OSError as exc:
                    self.warnings.append(f"/etc/{rel}: {exc}")

    def _export_usr_local_bin(self) -> None:
        ulb = self.ctx.roots.usr / "local" / "bin"
        if not ulb.is_dir():
            return
        target = self.meta_dir / "usr_local_bin"
        target.mkdir(parents=True, exist_ok=True)
        # Un symlink aquí casi siempre es una app instalada a mano en /opt (AppImages, esta misma
        # app...): demasiado variado para respaldarlo genérico; se anota el nombre para la lista
        # "software to install" en vez de descartarlo en silencio.
        skipped: List[str] = []
        for item in sorted(ulb.iterdir()):
            if item.is_symlink():
                try:
                    skipped.append(f"{item.name} -> {os.readlink(item)}")
                except OSError:
                    skipped.append(item.name)
                continue
            if item.is_file():
                try:
                    if item.stat().st_size <= MAX_BIN_BYTES:
                        shutil.copy2(item, target / item.name)
                except OSError as exc:
                    self.warnings.append(f"/usr/local/bin/{item.name}: {exc}")
        if skipped:
            (self.meta_dir / "usr_local_bin_symlinks.txt").write_text("\n".join(skipped) + "\n")

    def _export_keyd(self) -> None:
        self.ctx.copy(self.ctx.roots.etc / "keyd", "keyd")

    def _export_crontab(self) -> None:
        if not self.ctx.which("crontab"):
            return
        user = os.getenv("SUDO_USER")
        cmd = ["runuser", "-u", user, "--", "crontab", "-l"] if user else ["crontab", "-l"]
        r = self.ctx.run(cmd)
        if r.returncode == 0 and r.stdout.strip():
            (self.meta_dir / "crontab.txt").write_text(r.stdout)

    def _generate_restore_script(self) -> None:
        script = self.snapshot_dir / "restore.sh"
        script.write_text(self.pm.restore_script(self.engine))
        script.chmod(0o755)
