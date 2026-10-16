"""Unidades de backup: candidatas, punto de montaje real y tipo de filesystem.

Reglas de seguridad (README, regla 1):
  * NUNCA se cae a "la primera unidad escribible". Si ninguna califica, se devuelve None.
  * Un directorio que existe y se puede escribir NO basta: tiene que ser un punto de
    montaje real (os.path.ismount). Un directorio de montaje que quedó después de
    desenchufar el disco es escribible, pero vive en el disco raíz.
  * No hay ningún override para saltarse el chequeo de punto de montaje.

Lógica tomada de restore-tui (config.py: _candidate_drives / _autodetect_usb_target /
_looks_like_foreign_backup_drive) y btrfs-restore-tui (config.py: _fstype_of,
backup.py: _target_writable), con el chequeo ismount añadido en todos los caminos.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

# Carpeta que cada motor crea dentro de la unidad. No se migra nada (decisión 2).
DIRNAME_RSYNC = "restore-tui"
DIRNAME_BTRFS = "btrfs-restore"
ENGINE_DIRNAMES = {"rsync": DIRNAME_RSYNC, "btrfs": DIRNAME_BTRFS}

DEFAULT_TARGET_LABEL = "USB_DATA"

# Filesystems donde viven hardlinks y propietarios (motor rsync). exFAT/NTFS/vfat se rechazan.
RSYNC_TARGET_FSTYPES = {"ext4", "btrfs"}

IsMount = Callable[[Path], bool]


def _real_ismount(path: Path) -> bool:
    return os.path.ismount(str(path))


def fstype_of(
    path: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> Optional[str]:
    """Tipo de filesystem del mount que contiene `path` (findmnt), o None si no se sabe."""
    if not shutil.which("findmnt"):
        return None
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        res = run(
            ["findmnt", "-n", "-o", "FSTYPE", "-T", str(probe)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None


def default_bases(user: str) -> List[Path]:
    """Dónde monta el escritorio los discos removibles. /mnt NO está: no es removible."""
    return [Path(f"/run/media/{user}"), Path(f"/media/{user}")]


def other_engine_dirname(engine: str) -> str:
    return DIRNAME_BTRFS if engine == "rsync" else DIRNAME_RSYNC


def holds_other_edition_layout(drive: Path, engine: str) -> bool:
    """True si la unidad lleva el backup de la OTRA edición y ninguno nuestro.

    Reconoce los dos layouts: la carpeta actual de la otra edición
    (`btrfs-restore/` o `restore-tui/`) y el layout viejo de btrfs-restore-tui con
    home/ + root/ + meta/ en la raíz del mount.
    """
    own = ENGINE_DIRNAMES[engine]
    try:
        names = {p.name for p in drive.iterdir()}
    except OSError:
        return False
    if own in names:
        return False
    if other_engine_dirname(engine) in names:
        return True
    return {"home", "root", "meta"} <= names


def candidate_drives(
    user: str,
    engine: str,
    bases: Optional[Sequence[Path]] = None,
    is_mount: IsMount = _real_ismount,
) -> List[Path]:
    """Montajes removibles escribibles, REALES, que no son de la otra edición."""
    if bases is None:
        bases = default_bases(user)
    seen = set()
    out: List[Path] = []
    for base in bases:
        try:
            if not base.is_dir():
                continue
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry in seen:
                continue
            if entry.is_dir() and os.access(entry, os.W_OK) and is_mount(entry):
                seen.add(entry)
                out.append(entry)
    return [d for d in out if not holds_other_edition_layout(d, engine)]


def list_backup_drives(
    user: str,
    engine: str,
    bases: Optional[Sequence[Path]] = None,
    is_mount: IsMount = _real_ismount,
    fstype: Callable[[Path], Optional[str]] = fstype_of,
) -> List[Tuple[Path, Optional[str]]]:
    """Para la pantalla Settings: cada unidad candidata con su tipo de filesystem."""
    return [(d, fstype(d)) for d in candidate_drives(user, engine, bases, is_mount)]


def autodetect_target(
    user: str,
    engine: str,
    label: str = DEFAULT_TARGET_LABEL,
    bases: Optional[Sequence[Path]] = None,
    is_mount: IsMount = _real_ismount,
) -> Optional[str]:
    """Elige una unidad solo si YA tiene un backup nuestro o su etiqueta coincide.

    NO hay fallback a "la primera unidad que aparezca": escribir un backup completo
    en la partición equivocada es peor que parar con un mensaje.
    """
    usable = candidate_drives(user, engine, bases, is_mount)
    own = ENGINE_DIRNAMES[engine]
    for drive in usable:
        if (drive / own / "snapshots").is_dir():
            return str(drive)
    for drive in usable:
        if drive.name.upper() == label.upper():
            return str(drive)
    return None


def split_target(target_dir: str, engine: str) -> Tuple[Path, Path]:
    """(punto_de_montaje, raíz_del_backup) a partir de TARGET_DIR.

    TARGET_DIR puede ser el punto de montaje o la carpeta ya con sufijo.
    """
    own = ENGINE_DIRNAMES[engine]
    path = Path(target_dir)
    if path.name == own:
        return path.parent, path
    return path, path / own


def validate_target(
    target_dir: str,
    engine: str,
    is_mount: IsMount = _real_ismount,
    fstype: Callable[[Path], Optional[str]] = fstype_of,
) -> Tuple[bool, str]:
    """(ok, motivo). El punto de montaje real es obligatorio, sin excepciones.

    Además, el motor rsync solo escribe en ext4/btrfs (hardlinks y propietarios).
    """
    if not target_dir:
        return False, "no backup target configured"
    mount, _root = split_target(target_dir, engine)
    if not mount.is_dir():
        return False, f"{mount} does not exist (drive not mounted?)"
    if not is_mount(mount):
        return False, (
            f"{mount} is not a mount point; the drive is probably unplugged. "
            "Refusing to write to the system disk."
        )
    if engine == "rsync":
        fs = fstype(mount)
        if fs is None:
            return (
                False,
                f"cannot determine the filesystem of {mount}; refusing (rsync needs ext4 or btrfs)",
            )
        if fs not in RSYNC_TARGET_FSTYPES:
            return False, (
                f"{mount} is {fs}; the rsync engine needs ext4 or btrfs "
                "(hardlinks and file ownership)"
            )
    return True, str(mount)
