"""Pantalla de bienvenida: se muestra una vez (primer arranque interactivo) y desde el menú [W].

Nunca bloquea `backup-now` ni nada no interactivo: solo la llama el menú de `restore-tui`, y solo
si hay un terminal interactivo. La marca de "ya vista" es un archivo pequeño en el directorio de
estado del usuario (ver `restore-tui --paths`).
"""

import sys
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console

WELCOME_TEXT = """\
Welcome to restore-fusion

BACKUP FORMATS
  - btrfs snapshots   (Arch/Omarchy, or any system whose / and /home are btrfs)
  - rsync copy        (any system; versions kept with hardlinks)

DESTINATIONS
  - USB / local disk        ext4 or btrfs
  - Another computer        over SSH
      btrfs source: the server must be btrfs, or hold a btrfs image
      ext4 source:  no special requirement
  - Proxmox Backup Server   optional

NOT SUPPORTED
  - exFAT, NTFS, FAT disks  (no hardlinks or file permissions)
  - Network shares (SMB, NFS)
"""

NETWORK_SHARE_NOTICE = """\
Network shares (SMB/NFS) are not used directly: they usually cannot keep Linux
permissions or hardlinks. To back up to a NAS or another computer, use an SSH
server (Settings > Remote). Advanced: place a btrfs image on the share and use it
as a normal btrfs disk.
"""

STATE_FILE_NAME = "welcome-shown"


def state_file(home: Path) -> Path:
    """Estado del usuario (no config): ~/.local/state/restore-tui/welcome-shown."""
    return Path(home) / ".local" / "state" / "restore-tui" / STATE_FILE_NAME


def was_shown(home: Path) -> bool:
    try:
        return state_file(home).exists()
    except OSError:
        return True  # si no se puede leer, no molestar


def mark_shown(home: Path) -> None:
    """Best effort: un fallo al escribir nunca debe romper el menú."""
    try:
        f = state_file(home)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("shown\n")
    except OSError:
        pass


def show_welcome(
    out: Console,
    read: Optional[Callable[[], str]] = None,
    *,
    home: Optional[Path] = None,
) -> bool:
    """Imprime la bienvenida y espera Enter. True si el usuario la cerró (y se recuerda);
    False si no hubo entrada (EOF/Ctrl-C): entonces no se marca como vista."""
    out.print()
    out.print(WELCOME_TEXT, markup=False, highlight=False)
    out.print(NETWORK_SHARE_NOTICE, markup=False, highlight=False)
    read = read or (lambda: out.input("[ Continue ] press Enter "))
    try:
        read()
    except (EOFError, KeyboardInterrupt):
        return False
    if home is not None:
        mark_shown(home)
    return True


def first_run(
    out: Console,
    home: Path,
    *,
    interactive: Optional[bool] = None,
    read: Optional[Callable[[], str]] = None,
) -> bool:
    """Primer arranque: muestra la bienvenida solo si es interactivo y no se vio antes."""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive or was_shown(home):
        return False
    return show_welcome(out, read, home=home)
