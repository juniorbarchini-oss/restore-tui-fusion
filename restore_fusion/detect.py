"""Detección: filesystem de origen/destino -> motor de copia; os-release -> gestor de paquetes.

El motor y el gestor de paquetes son decisiones independientes: un Ubuntu con raíz btrfs
usa el motor btrfs con apt.

`ENGINE=auto|rsync|btrfs` (decisión 3): `auto` elige btrfs si `/` y `/home` son subvolúmenes
btrfs y existe la herramienta; si no, rsync. rsync sobre un origen btrfs solo con
`ENGINE=rsync` explícito. Un override que `available()` no acepta falla; nunca se cambia
de motor en silencio.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .drives import fstype_of, validate_target

VALID_ENGINE_SETTINGS = ("auto", "rsync", "btrfs")

# Raíz de un subvolumen btrfs: inode 256.
_BTRFS_SUBVOL_INODE = 256

# ID / ID_LIKE de /etc/os-release
_PACMAN_IDS = {"arch", "archarm", "manjaro", "endeavouros", "cachyos", "omarchy"}
_APT_IDS = {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "elementary"}


class EngineError(RuntimeError):
    """El motor pedido (o ninguno) no sirve para este sistema; el mensaje dice por qué."""


@dataclass(frozen=True)
class EngineChoice:
    engine: str  # "rsync" | "btrfs"
    reason: str


def is_btrfs_subvolume(
    path: Path,
    fstype: Callable[[Path], Optional[str]] = fstype_of,
    stat: Callable[[str], os.stat_result] = os.stat,
) -> bool:
    try:
        if fstype(path) != "btrfs":
            return False
        return stat(str(path)).st_ino == _BTRFS_SUBVOL_INODE
    except OSError:
        return False


def source_is_btrfs(
    mounts: Sequence[str] = ("/", "/home"),
    is_subvolume: Callable[[Path], bool] = is_btrfs_subvolume,
) -> bool:
    return all(is_subvolume(Path(m)) for m in mounts)


def choose_engine(
    setting: str = "auto",
    *,
    source_btrfs: bool,
    btrfs_tool: bool,
    target_dir: Optional[str] = None,
    is_mount: Optional[Callable[[Path], bool]] = None,
    fstype: Callable[[Path], Optional[str]] = fstype_of,
) -> EngineChoice:
    """Elige el motor y comprueba que el destino lo acepte.

    Si `target_dir` es None no se valida el destino (por ejemplo, solo hay SSH).
    """
    setting = (setting or "auto").strip().lower()
    if setting not in VALID_ENGINE_SETTINGS:
        raise EngineError(f"ENGINE={setting!r} is not valid (use auto, rsync or btrfs)")

    if setting == "btrfs":
        if not btrfs_tool:
            raise EngineError("ENGINE=btrfs but btrfs-progs is not installed")
        if not source_btrfs:
            raise EngineError("ENGINE=btrfs but / and /home are not both btrfs subvolumes")
        choice = EngineChoice("btrfs", "ENGINE=btrfs set explicitly")
    elif setting == "rsync":
        choice = EngineChoice("rsync", "ENGINE=rsync set explicitly")
    elif source_btrfs and btrfs_tool:
        choice = EngineChoice("btrfs", "/ and /home are btrfs subvolumes")
    elif source_btrfs:
        # decisión 3: rsync en un origen btrfs solo con ENGINE=rsync explícito
        raise EngineError(
            "/ and /home are btrfs but btrfs-progs is not installed; "
            "install it, or set ENGINE=rsync explicitly"
        )
    else:
        choice = EngineChoice("rsync", "source is not btrfs")

    if target_dir is not None:
        kwargs = {"fstype": fstype}
        if is_mount is not None:
            kwargs["is_mount"] = is_mount
        ok, why = validate_target(target_dir, choice.engine, **kwargs)
        if not ok:
            raise EngineError(why)
    return choice


def btrfs_tool_present() -> bool:
    return bool(shutil.which("btrfs"))


def parse_os_release(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def detect_package_manager(os_release_text: str) -> Optional[str]:
    """'apt' | 'pacman' | None según ID / ID_LIKE. None = distro no soportada."""
    info = parse_os_release(os_release_text)
    ids = [info.get("ID", "").lower(), *info.get("ID_LIKE", "").lower().split()]
    for i in ids:
        if i in _PACMAN_IDS:
            return "pacman"
        if i in _APT_IDS:
            return "apt"
    return None


def read_os_release(path: str = "/etc/os-release") -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def package_manager_here() -> Tuple[Optional[str], str]:
    pm = detect_package_manager(read_os_release())
    return pm, ("detected from /etc/os-release" if pm else "unsupported or unknown distro")
