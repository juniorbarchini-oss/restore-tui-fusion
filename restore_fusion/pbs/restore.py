"""`restore-tui pbs-restore` / menú [X]: explora un snapshot guardado en PBS y restaura de él.

Usa el MISMO explorador y el MISMO motor de restauración que `restore-now` (M6/M12): marcar con
Space, [R] al original, [E] a otra carpeta, Backup (.bak) / Overwrite / Skip, lista completa de
errores. Solo cambia el origen: un montaje FUSE de solo lectura de `root.pxar` / `home.pxar`.
Ninguna selección por defecto: el snapshot se elige por número, sin "Enter = el más nuevo".
"""

import atexit
import os
import shutil
import signal
import sys
from typing import Callable, List, Optional, Sequence

from rich.console import Console

from .client import CLIENT_HINT, TOOL, PbsClient, PbsError, check_configured, PbsSnapshot
from .mount import PbsServices

console = Console()


def pick_snapshot(
    snapshots: List[PbsSnapshot], ask: Callable[[str], str] = None, out: Console = console
) -> Optional[PbsSnapshot]:
    """Elige por número. Vacío / inválido / sin terminal => None (nada se elige por el usuario)."""
    if not snapshots:
        out.print("[yellow]No snapshots found for this host on PBS.[/]")
        return None
    out.print("\n[bold #00FF66]Snapshots on PBS (newest first):[/]\n")
    for i, s in enumerate(snapshots):
        out.print(
            f"  [{i}] {s.local_time:%Y-%m-%d %H:%M:%S}  ({s.size / 1024**3:.2f} GiB)", markup=False
        )
    ask = ask or (lambda p: out.input(p))
    try:
        raw = ask("\nPick a snapshot number: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw.isdigit() or int(raw) >= len(snapshots):
        out.print("[yellow]No valid snapshot picked - nothing mounted.[/]")
        return None
    return snapshots[int(raw)]


def _unwind_on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def run_restore(cfg, *, client: Optional[PbsClient] = None, ask=None, app_factory=None,
                hostname: Optional[str] = None, base=None) -> int:  # fmt: skip
    help_text = check_configured(cfg)
    if help_text:
        console.print(f"[yellow]{help_text}[/]")
        return 1
    client = client or PbsClient(cfg)
    if not client.installed():
        console.print(f"[red]{TOOL} not found.[/] {CLIENT_HINT}")
        return 1
    hostname = hostname or os.uname().nodename
    try:
        snaps = client.list_snapshots(hostname)
    except PbsError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    chosen = pick_snapshot(snaps, ask)
    if chosen is None:
        return 1

    services = PbsServices(
        cfg, client, hostname, base=base, only=chosen
    )  # la app muestra solo el elegido
    atexit.register(services.cleanup)
    if app_factory is None:
        from ..restore.engine import RestoreEngine
        from ..ui.restore_app import RestoreApp

        def app_factory(svc):
            return RestoreApp(svc, RestoreEngine(user=cfg.user, home=cfg.home), user_home=cfg.home)

    try:
        app_factory(services).run()
    finally:
        services.cleanup()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from .. import config as cfgmod
    from ..cli_backup import maybe_elevate
    from ..detect import EngineError, btrfs_tool_present, choose_engine, source_is_btrfs

    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = cfgmod.load()
    try:
        engine_id = choose_engine(
            cfg.engine, source_btrfs=source_is_btrfs(cfg.source_mounts),
            btrfs_tool=btrfs_tool_present(),
        ).engine  # fmt: skip
    except EngineError as exc:
        console.print(f"[bold red]{exc}[/]")
        return 2
    if maybe_elevate(engine_id, argv, module="restore_fusion.pbs.restore"):
        return 0
    try:
        signal.signal(signal.SIGTERM, _unwind_on_signal)
        signal.signal(signal.SIGHUP, _unwind_on_signal)
    except (ValueError, OSError):
        pass
    return run_restore(cfg)


if __name__ == "__main__":
    sys.exit(main())
