"""`restore-now`: la pantalla de dos paneles para navegar un snapshot y restaurar archivos/carpetas.

Privilegios diferidos: el menú corre sin privilegios; esta pantalla se re-ejecuta bajo sudo SOLO si
el sistema es btrfs (restaurar a `/` y recibir streams lo necesita). En un sistema ext4/rsync corre
con el usuario normal.
"""

import atexit
import signal
import sys
from typing import Optional, Sequence

from rich.console import Console
from rich.panel import Panel

from . import config as cfgmod
from .cli_backup import maybe_elevate
from .detect import EngineError, btrfs_tool_present, choose_engine, source_is_btrfs

console = Console()


def _unwind_on_signal(signum, _frame):
    """Solo lanza: atexit y on_unmount hacen la limpieza (montajes sshfs, staging)."""
    raise SystemExit(128 + signum)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = cfgmod.load()
    try:
        engine_id = choose_engine(
            cfg.engine,
            source_btrfs=source_is_btrfs(cfg.source_mounts),
            btrfs_tool=btrfs_tool_present(),
        ).engine
    except EngineError as exc:
        console.print(Panel(f"[bold red]{exc}[/bold red]", border_style="red"))
        return 2
    if maybe_elevate(engine_id, argv, module="restore_fusion.cli_restore"):
        return 0

    from .restore.engine import RestoreEngine
    from .restore.services import RestoreServices
    from .ui.restore_app import RestoreApp

    services = RestoreServices(cfg)
    atexit.register(services.cleanup)
    try:
        signal.signal(signal.SIGTERM, _unwind_on_signal)
        signal.signal(signal.SIGHUP, _unwind_on_signal)
    except (ValueError, OSError):
        pass
    if engine_id == "btrfs":
        services.staging.sweep_orphans()
    services.mounter.sweep_orphans()
    RestoreApp(services, RestoreEngine(user=cfg.user, home=cfg.home), user_home=cfg.home).run()
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
