"""PATH hermético para los tests que ejecutan restore.sh / install.sh / uninstall.sh.

Problema que resuelve: con `PATH = falsos + PATH real`, en una máquina que SÍ tiene `yay`, `sudo`,
`flatpak`, `systemctl`... el script llamaba al binario real (colgado esperando una contraseña, o peor,
instalando paquetes). Ahora el PATH es:   <dir de falsos>  :  <dir seguro con enlaces a lo básico>
y NADA más. Todo comando peligroso tiene un doble en el dir de falsos (por defecto sale con 0 y deja
huella en `calls.log`); un test puede sustituir el que le interese con `fake_bin(...)`.
"""

import os
import shutil
import stat
from pathlib import Path
from typing import Dict, Iterable, Optional

# Comandos que JAMÁS deben resolverse al binario del host (gestores de paquetes, privilegios, servicios...).
DANGEROUS = (
    "yay", "paru", "pacman", "apt", "apt-get", "dpkg", "dnf", "snap", "flatpak", "sudo", "su", "doas",
    "systemctl", "service", "dconf", "gsettings", "gnome-extensions", "crontab", "arch-chroot", "btrfs",
    "mount", "umount", "fusermount", "fusermount3", "sshfs", "ssh", "scp", "notify-send", "runuser",
    "omarchy", "pip", "pip3", "chown", "chroot", "reboot", "shutdown", "mkinitcpio", "update-grub",
)  # fmt: skip

# Herramientas básicas que los scripts sí necesitan de verdad (se enlazan desde el host).
BASIC = (
    "bash", "sh", "env", "cat", "cp", "mv", "rm", "ln", "mkdir", "rmdir", "ls", "chmod", "touch", "tee",
    "date", "dirname", "basename", "readlink", "realpath", "stat", "sort", "uniq", "head", "tail", "wc",
    "tr", "cut", "sed", "awk", "grep", "find", "xargs", "id", "whoami", "getent", "mktemp", "install",
    "tar", "sleep", "true", "false", "test", "printf", "echo", "uname", "hostname", "df", "du", "diff",
    "cmp", "rsync", "python3", "findmnt", "zstd", "pgrep", "kill", "ps", "nproc", "sync", "dd",
)  # fmt: skip

_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "${HERMETIC_LOG:-/dev/null}"\nexit 0\n'


def _fake_id(real_id: str) -> str:
    """`id -u` / `id -ru` dicen 0: los scripts creen que YA son root y no hacen `exec sudo -E "$0"`
    (con un sudo falso que reejecuta el script, como usuario normal sería un bucle infinito). El resto
    de argumentos va al `id` real."""
    return (
        "#!/bin/sh\n" 'case "$*" in "-u"|"-ru") echo 0; exit 0;; esac\n' f'exec "{real_id}" "$@"\n'
    )


def fake_bin(
    d: Path, scripts: Optional[Dict[str, str]] = None, stubs: Iterable[str] = DANGEROUS
) -> Path:
    """Crea `d` con un doble inofensivo por cada comando peligroso y, encima, los `scripts` pedidos."""
    d.mkdir(parents=True, exist_ok=True)
    real_id = shutil.which("id")
    if real_id and "id" not in (scripts or {}) and not (d / "id").exists():
        (d / "id").write_text(_fake_id(real_id))
        (d / "id").chmod(0o755)
    for name in stubs:
        p = d / name
        if not p.exists():
            p.write_text(_STUB)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
    for name, body in (scripts or {}).items():
        p = d / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def safe_dir(d: Path, extra: Iterable[str] = ()) -> Path:
    """Dir con enlaces SOLO a las herramientas básicas del host (nunca a algo de DANGEROUS)."""
    d.mkdir(parents=True, exist_ok=True)
    for name in (*BASIC, *extra):
        if name in DANGEROUS:
            raise ValueError(f"{name} is dangerous and cannot be linked from the host")
        real = shutil.which(name)
        if real and not (d / name).exists():
            (d / name).symlink_to(real)
    return d


def hermetic_path(root: Path, scripts: Optional[Dict[str, str]] = None, extra: Iterable[str] = ()):
    """(PATH, dir de falsos). `root` es un directorio temporal del test."""
    fakes = fake_bin(root / "fakebin", scripts)
    safe = safe_dir(root / "safebin", extra)
    return f"{fakes}{os.pathsep}{safe}", fakes


def resolve(name: str, path: str) -> Optional[str]:
    return shutil.which(name, path=path)


def escaped_commands(path: str, fakes: Path) -> Dict[str, str]:
    """Comandos peligrosos que, con este PATH, NO se resuelven dentro del dir de falsos."""
    out = {}
    for name in DANGEROUS:
        got = resolve(name, path)
        if got is not None and Path(got).parent != Path(fakes):
            out[name] = got
    return out
