"""`restore-tui`: punto de entrada único. Sin argumentos muestra el menú retro:

    [B]  Backup now
    [R]  Restore files / folders
    [S]  Settings (engine, backup drive, SSH remote)
    [D]  Destinations (pick a drive or an SSH computer)
    [P]  Push to Proxmox (PBS)       (listed only if PBS is configured or the client is installed)
    [X]  Restore from Proxmox (PBS)  (same)
    [F]  Full recovery (freshly installed machine)
    [W]  Welcome / what is supported
    [U]  Update (installed copies: looks for and installs a newer version)
    [Q]  Quit

`restore-tui backup [...]` salta directo al
modo. Cada modo corre como proceso hijo, así que el control vuelve al menú. El menú y el
estado corren sin privilegios; el modo que lo necesita (motor btrfs) se re-ejecuta bajo sudo
SOLO él (ver cli_backup.maybe_elevate).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import config as cfgmod
from . import update, welcome, wizard
from .detect import (
    btrfs_tool_present,
    choose_engine,
    EngineError,
    package_manager_here,
    source_is_btrfs,
)

PBS_TOOL = "proxmox-backup-client"
PBS_KEYS = ("p", "x")
_PKG_ROOT = Path(__file__).resolve().parent.parent
console = Console()


def _run_mode(module: str, extra_args: Optional[List[str]] = None) -> int:
    """Corre un modo como proceso hijo y devuelve su código de salida."""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{_PKG_ROOT}:{env.get('PYTHONPATH', '')}".rstrip(":")
    try:
        return subprocess.run(
            [sys.executable, "-m", module, *(extra_args or [])], env=env
        ).returncode
    except KeyboardInterrupt:
        return 130


def _run_backup(argv=None) -> int:
    return _run_mode("restore_fusion.cli_backup", argv or [])


MODES: Dict[str, str] = {
    "r": "restore_fusion.cli_restore",
    "s": "restore_fusion.ui.settings",
    "p": "restore_fusion.pbs.push",
    "x": "restore_fusion.pbs.restore",
}  # clave de menú -> módulo


def _pbs_visible(cfg=None, which=None) -> bool:
    """[P]/[X] se listan si PBS está configurado O el cliente está instalado ("ni lo uno ni lo
    otro" = ocultas). Con una sola de las dos cosas siguen visibles: los mensajes existentes
    explican qué falta. Las teclas funcionan igual aunque estén ocultas."""
    cfg = cfg or cfgmod.load()
    which = which or shutil.which
    configured = bool(cfg.pbs_repository and cfg.pbs_password)
    return configured or which(PBS_TOOL) is not None


def _engine_id() -> Optional[str]:
    try:
        return choose_engine(
            cfgmod.load().engine, source_btrfs=source_is_btrfs(), btrfs_tool=btrfs_tool_present()
        ).engine
    except EngineError:
        return None  # sin motor definido el asistente lista las unidades de ambos motores


def _destinations_wizard() -> None:
    """[D] y primer arranque. Solo interactivo; nunca escribe sin un resumen confirmado."""
    wizard.run_wizard(console, cfgmod.load(), engine=_engine_id())


def _engine_label() -> str:
    try:
        return choose_engine(
            cfgmod.load().engine, source_btrfs=source_is_btrfs(), btrfs_tool=btrfs_tool_present()
        ).engine
    except EngineError as exc:
        return f"? ({exc})"


def _show_recovery_info() -> None:
    cfg = cfgmod.load()
    engine = _engine_label()
    pm, _ = package_manager_here()
    folder = "btrfs-restore" if engine == "btrfs" else "restore-tui"
    body = Text()
    body.append("BARE-METAL RECOVERY\n\n", style="bold #00FF66")
    body.append("Each backup snapshot carries a self-contained ", style="#00FF66")
    body.append("restore.sh", style="bold yellow")
    body.append(
        " that reinstalls\nyour packages and restores your files - it needs only bash, coreutils, "
        "rsync and\nthe package manager (no Python, no this app).\n\n",
        style="#00FF66",
    )
    body.append(f"From a freshly installed machine ({pm or 'same distro'}):\n", style="#00FF66")
    body.append(
        f"  1.  mount the backup drive  ({cfg.target_dir or '<drive>'})\n", style="dim white"
    )
    body.append(f"  2.  cd <drive>/{folder}\n", style="dim white")
    body.append(
        "  3.  ./disaster-recovery.sh        # lists the snapshots, you pick one\n",
        style="dim white",
    )
    body.append("\nTo recover just a few files instead, use ", style="#00FF66")
    body.append("Restore", style="bold yellow")
    body.append(" from the menu.", style="#00FF66")
    console.print(Panel(body, border_style="#00FF66", title="[bold #00FF66]Full recovery[/]"))


def _print_menu() -> None:
    host = os.uname().nodename
    cfg = cfgmod.load()
    show_pbs = _pbs_visible(cfg)
    dest = (
        cfg.target_dir
        or (cfg.remote_name if cfg.remote_configured else None)
        or "(no target - mount the drive)"
    )
    header = Text()
    header.append("  RESTORE TUI\n", style="bold #00FF66")
    header.append(
        f"  Source: {host}   Engine: {_engine_label()}   Target: {dest}\n\n", style="dim #00FF66"
    )
    header.append("   [B]  Backup now\n", style="#00FF66")
    for key, label in (
        ("R", "Restore files / folders"),
        ("S", "Settings"),
        ("P", "Push to Proxmox (PBS)"),
        ("X", "Restore from Proxmox (PBS)"),
    ):
        if key.lower() in MODES and (show_pbs or key.lower() not in PBS_KEYS):
            header.append(f"   [{key}]  {label}\n", style="#00FF66")
    header.append("   [D]  Destinations (drive or SSH computer)\n", style="#00FF66")
    header.append("   [F]  Full recovery (freshly installed machine)\n", style="#00FF66")
    header.append("   [W]  Welcome / what is supported\n", style="#00FF66")
    if update.is_installed():
        newer = update.available(Path(cfg.home), cfg.check_updates)
        header.append("   [U]  Update", style="#00FF66")
        header.append(
            f"  (new version {newer[:7]} available)\n" if newer else "\n", style="bold yellow"
        )
    header.append("   [Q]  Quit", style="#00FF66")
    console.print(Panel(header, border_style="#00FF66"))


def _menu() -> int:
    try:  # solo el menú interactivo; nunca backup-now ni subcomandos
        if welcome.first_run(console, Path(cfgmod.load().home)):
            _destinations_wizard()  # justo tras la bienvenida; "Skip for now" es una opción
    except Exception:  # la bienvenida jamás debe impedir usar el menú
        pass
    _print_menu()
    while True:
        try:
            choice = console.input("[bold #00FF66]> [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ("b", "backup"):
            _run_backup()
        elif choice in ("w", "welcome"):
            welcome.show_welcome(console, home=Path(cfgmod.load().home))
            _print_menu()
            continue
        elif choice in ("d", "destinations"):
            try:
                _destinations_wizard()
            except Exception as exc:  # noqa: BLE001 - el asistente jamás debe tirar el menú
                console.print(f"[yellow]  The destinations assistant failed: {exc}[/]")
            _print_menu()  # el destino mostrado pudo cambiar
            continue
        elif choice in ("u", "update"):
            if update.run_update(console, Path(cfgmod.load().home)):
                return 0  # the running code was just replaced
        elif choice in ("f", "full"):
            _show_recovery_info()
            continue
        elif choice in MODES:
            _run_mode(MODES[choice])
        elif choice in ("q", "quit", ""):
            return 0
        else:
            show_pbs = _pbs_visible()
            keys = [
                "B",
                *[k.upper() for k in MODES if show_pbs or k not in PBS_KEYS],
                "D",
                "F",
                "W",
                *(["U"] if update.is_installed() else []),
                "Q",
            ]
            console.print(f"[yellow]  Pick one: {', '.join(keys)}[/]")
            continue
        console.print()
        _print_menu()


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("backup", "backup-now"):
        return _run_backup(args[1:])
    if args and args[0] in ("restore", "restore-now"):
        return _run_mode(MODES["r"])
    if args and args[0] == "settings":
        return _run_mode(MODES["s"])
    if args and args[0] == "pbs":
        return _run_mode(MODES["p"])
    if args and args[0] == "pbs-restore":
        return _run_mode(MODES["x"])
    if args and args[0] in ("--gc", "--paths"):
        return _run_mode("restore_fusion.maintenance", args)
    if args and args[0] in ("-h", "--help"):
        console.print(__doc__ or "")
        console.print("  restore-tui --gc      remove staging / scratch from dead runs")
        console.print("  restore-tui --paths   list every path the tool touches")
        return 0
    return _menu()


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
