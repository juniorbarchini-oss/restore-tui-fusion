"""Asistente de destinos (consola `rich`, no Textual). Se ofrece tras la bienvenida del primer
arranque y desde el menú [D].

Reglas: nada preseleccionado (el usuario escribe un número o una letra, Enter vacío no elige nada),
no escribe NADA hasta que se confirma un resumen (el Enter por defecto es "No"), no formatea, no
borra, no monta, nunca cambia el motor, y no corre sin terminal interactivo (jamás frena backup-now).
Escribe solo las claves que muestra el resumen, con `save_user_settings`.
"""

import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from rich.console import Console
from rich.markup import escape

from . import drives as drivesmod
from .destinations.ssh import SSHDestination, remote_fs_message, remote_fs_type

# Discos sin hardlinks ni permisos Linux: no se usan con ningún motor (ver la bienvenida).
UNSUPPORTED_FSTYPES = {"vfat", "fat", "fat32", "exfat", "ntfs", "ntfs3", "fuseblk", "msdos"}

Read = Callable[[str], str]


def drive_flag(fstype: Optional[str], engine: Optional[str]) -> Tuple[bool, str]:
    """(usable, nota). Con el motor btrfs un disco no-btrfs funciona (streams .btrfs.zst): es un
    aviso, no un bloqueo. Con rsync solo sirven ext4/btrfs (misma regla que validate_target)."""
    if fstype is None:
        if engine == "rsync":
            return False, "filesystem unknown - rsync needs ext4 or btrfs"
        return True, "filesystem unknown"
    if fstype in UNSUPPORTED_FSTYPES:
        return False, f"{fstype} - not supported (no hardlinks or file permissions)"
    if engine == "rsync" and fstype not in drivesmod.RSYNC_TARGET_FSTYPES:
        return False, f"{fstype} - the rsync engine needs ext4 or btrfs"
    if engine == "btrfs" and fstype != "btrfs":
        return True, f"{fstype} - snapshots will be saved as .btrfs.zst stream files"
    return True, ""


def detect_drives(user: str, engine: Optional[str], **kw) -> List[Tuple[Path, Optional[str]]]:
    """Unidades montadas de verdad. Sin motor conocido: las de cualquiera de los dos (sin repetir)."""
    seen, out = set(), []
    for eng in (engine,) if engine else ("rsync", "btrfs"):
        for d, fs in drivesmod.list_backup_drives(user, eng, **kw):
            if d not in seen:
                seen.add(d)
                out.append((d, fs))
    return sorted(out)


def _ask(read: Read, prompt: str) -> Optional[str]:
    try:
        return read(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _confirm(out: Console, read: Read, lines: List[str]) -> bool:
    out.print("\nSummary - nothing has been written yet:")
    for line in lines:
        out.print(f"  {line}", markup=False, highlight=False)
    answer = _ask(read, "Write these settings? [y/N] ")
    return bool(answer) and answer.lower() in ("y", "yes")


def _ssh_flow(out, read, cfg, engine, remote_fs, saver) -> bool:
    out.print("\nAnother computer over SSH. Leave host empty to go back.")
    host = _ask(read, "Host: ")
    if not host:
        return False
    user = _ask(read, "User: ")
    path = _ask(read, "Remote path: ")
    port_raw = _ask(read, "Port (Enter = 22): ")
    if not user or not path or port_raw is None:
        out.print("[yellow]User and remote path are required; nothing written.[/]")
        return False
    if port_raw and not (port_raw.isdigit() and 0 < int(port_raw) < 65536):
        out.print("[yellow]The port must be a number from 1 to 65535; nothing written.[/]")
        return False
    port = int(port_raw or 22)
    out.print(f"Checking the remote filesystem of {path} (read-only) ...")
    fs = remote_fs(host, user, path, port)
    if fs is None:
        out.print("Could not determine the remote filesystem; continuing.", markup=False)
    else:
        out.print(f"Remote filesystem: {fs}", markup=False, highlight=False)
    if engine == "btrfs":
        note = remote_fs_message("SSH server", path, fs)
        if note and fs is not None:
            out.print(f"[yellow]{escape(note)}[/]", highlight=False)
    if not _confirm(out, read, [f"SSH server: {user}@{host}:{port} {path}", "REMOTE_ENABLED=true"]):
        out.print("Nothing written.")
        return False
    saver({"REMOTE_ENABLED": "true", "REMOTE_HOST": host, "REMOTE_USER": user,
           "REMOTE_PATH": path, "REMOTE_PORT": str(port)})  # fmt: skip
    out.print("Saved. Run backup-now --dry-run to see what a backup would do.")
    return True


def default_remote_fs(cfg) -> Callable[[str, str, str, int], Optional[str]]:
    def check(host, user, path, port):
        dest = SSHDestination(host=host, root=path, user=user, port=port, home=Path(cfg.home))
        return remote_fs_type(dest)

    return check


def run_wizard(
    out: Console,
    cfg,
    *,
    engine: Optional[str] = None,
    read: Optional[Read] = None,
    drives: Optional[List[Tuple[Path, Optional[str]]]] = None,
    remote_fs: Optional[Callable[[str, str, str, int], Optional[str]]] = None,
    saver: Optional[Callable[[dict], Path]] = None,
    is_mount: drivesmod.IsMount = drivesmod._real_ismount,
    interactive: Optional[bool] = None,
) -> bool:
    """True si se guardó algo. Sin terminal interactivo no pregunta ni escribe nada."""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        return False
    if saver is None:
        from .config import save_user_settings

        saver = save_user_settings
    read = read or (lambda prompt: out.input(prompt))
    remote_fs = remote_fs or default_remote_fs(cfg)
    found = drives if drives is not None else detect_drives(cfg.user, engine)

    out.print("\nWhere should backups go?")
    for i, (drive, fs) in enumerate(found, 1):
        usable, note = drive_flag(fs, engine)
        flag = f"  [{'' if usable else 'CANNOT BE USED: '}{note}]" if note else ""
        out.print(f"  {i}  {drive}  ({fs or 'unknown'}){flag}", markup=False, highlight=False)
    if not found:
        out.print("  (no removable drive found - insert one and open [D] again, or pick below)")
    out.print("  A  Another computer over SSH")
    out.print("  S  Skip for now")
    while True:
        choice = _ask(read, "Your choice (nothing is preselected): ")
        if choice is None or choice.lower() == "s":
            out.print("Skipped. Nothing written.")
            return False
        if choice.lower() == "a":
            return _ssh_flow(out, read, cfg, engine, remote_fs, saver)
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            drive, fs = found[int(choice) - 1]
            usable, note = drive_flag(fs, engine)
            if not usable:
                out.print(f"[yellow]That drive cannot be used: {escape(note)}. Pick another.[/]")
                continue
            if note:
                out.print(f"[yellow]{escape(note)}[/]")
            ok, why = drivesmod.validate_target(str(drive), "btrfs", is_mount=is_mount)
            if not ok:  # el mismo chequeo de punto de montaje real que usa el backup
                out.print(f"[yellow]{escape(why)}[/]")
                continue
            if not _confirm(out, read, [f"TARGET_DIR={drive}"]):
                out.print("Nothing written.")
                return False
            saver({"TARGET_DIR": str(drive)})
            out.print("Saved.")
            return True
        out.print("[yellow]Type a drive number, A or S.[/]")
