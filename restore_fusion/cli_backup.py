"""`backup-now`: copia de $HOME / del sistema a la unidad USB y/o al host SSH, con el motor que
corresponda (rsync+hardlinks o btrfs send/receive) y el panel retro en vivo.

De btrfs-restore-tui/cli_backup.py y restore-tui/cli_backup.py. Reglas (README):
  * Un destino USB sale de `resolve_target`: punto de montaje real, jamás "la primera unidad".
  * Antes de un backup COMPLETO se pregunta (`--yes` lo salta); si el remoto no se puede consultar
    se dice y NUNCA se asume "vacío" (con --yes ese destino se omite).
  * Borrar copias remotas a medias pide un `y` explícito: ni --yes ni sin terminal lo aprueban.
  * La retención configurada pasa por plan + Approval y se muestra en --dry-run.
  * El motor btrfs necesita root: solo entonces se re-ejecuta bajo sudo.
"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from . import config as cfgmod
from .backup.notify import clear_alert, send_alert
from .backup.runner import BackupRunner, RunSummary
from .backup.targets import candidate_destinations
from .destinations.ssh import SSHDestination
from .detect import (
    EngineError, btrfs_tool_present, choose_engine, package_manager_here, source_is_btrfs,
)  # fmt: skip
from .engines.base import FullCheck
from .packages import manager_for
from .safety import ask_delete, confirm, confirm_action

console = Console(
    theme=Theme({
        "info": "green", "warning": "yellow", "error": "bold red", "success": "bold green",
        "progress": "bright_green", "stage": "bold bright_green",
    })  # fmt: skip
)

PRESERVE_ENV = "PYTHONPATH,TERM,COLORTERM,WAYLAND_DISPLAY,DISPLAY,XDG_RUNTIME_DIR"


def make_engine(engine_id: str, cfg):
    if engine_id == "btrfs":
        from .engines.btrfs import BtrfsSendEngine

        return BtrfsSendEngine(cfg)
    from .engines import RsyncHardlinkEngine

    return RsyncHardlinkEngine()


def maybe_elevate(
    engine_id: str,
    argv: Sequence[str],
    *,
    module: str = "restore_fusion.cli_backup",
    geteuid=os.geteuid,
    execvp=os.execvp,
    environ=os.environ,
) -> bool:
    """El motor btrfs necesita root: re-ejecuta ESTE comando bajo sudo. True si lo intentó."""
    if engine_id != "btrfs" or geteuid() == 0:
        return False
    keep = PRESERVE_ENV + "".join(f",{k}" for k in environ if k.startswith("RESTORE_TUI_"))
    console.print("[dim]btrfs needs administrative privileges - requesting sudo...[/dim]")
    execvp(
        "sudo",
        [
            "sudo",
            f"--preserve-env={keep}",
            sys.executable,
            "-m",
            module,
            *argv,
        ],
    )
    return True


def _header(cfg, engine_id: str, dry_run: bool) -> Panel:
    text = Text()
    text.append(
        "restore-fusion - BACKUP" + ("  (DRY RUN)\n" if dry_run else "\n"), style="bold green"
    )
    text.append(
        f"Source:      {cfg.home if engine_id == 'rsync' else ' '.join(cfg.source_mounts)}\n",
        style="white",
    )
    text.append(f"Engine:      {engine_id}\n", style="dim green")
    text.append(f"Config:      {cfg.config_source}\n", style="dim white")
    cap = f", max {cfg.max_snapshots}" if cfg.max_snapshots else ""
    text.append(
        f"Retention:   drive <= {cfg.max_disk_percent}%, keep >= {cfg.min_keep}{cap}",
        style="dim white",
    )
    return Panel(text, border_style="green")


def confirm_full(dest, engine, assume_yes: bool) -> bool:
    """¿Seguir con este destino? Avisa si sería un backup COMPLETO o si no se pudo comprobar."""
    check = engine.would_be_full(dest)
    if check is FullCheck.NO:
        return True
    where = dest.display_root()
    if check is FullCheck.YES:
        console.print(Panel(
            f"[bold yellow]{dest.label}: this would be a FULL backup[/bold yellow] - no previous "
            f"snapshot found there.\nDestination: [bold]{where}[/bold]\n\nNormal for the first backup "
            "to a target. If you expected a fast incremental, stop and check the drive / host.",
            border_style="yellow", title="[yellow]Confirm[/yellow]"))  # fmt: skip
        return confirm_action(
            f"Continue with a full backup to {dest.label}?", assume_yes=assume_yes
        )
    console.print(Panel(
        f"[bold yellow]{dest.label}: could not check for previous snapshots[/bold yellow]\n"
        f"Destination: [bold]{where}[/bold]\n\nContinuing would send a FULL backup. If you expected a "
        "fast incremental, stop and check the network / host.",
        border_style="yellow", title="[yellow]Confirm[/yellow]"))  # fmt: skip
    if assume_yes:  # --yes jamás convierte "no pude preguntar" en un envío a ciegas
        console.print(f"[dim]{dest.label} skipped: cannot verify what it holds.[/dim]")
        return False
    return confirm(f"Continue with a full backup to {dest.label}?")


def ask_heal(dest, engine) -> Optional[object]:
    """Copias remotas a medias (btrfs): se ofrece borrarlas; solo un `y` explícito las aprueba."""
    if not (engine.id == "btrfs" and isinstance(dest, SSHDestination)):
        return None
    try:
        partial = engine.partial_copies(dest)
    except Exception:  # noqa: BLE001 - el chequeo de backup completo ya informó el motivo
        return None
    if not partial:
        return None
    deletable = [p for p in partial if not p["has_children"]]
    lines = [f"  {p['kind']}/{p['name']}" + ("  (kept: other snapshots depend on it)" if p["has_children"] else "") for p in partial]  # fmt: skip
    console.print(Panel(
        f"[bold yellow]{dest.label}: {len(partial)} partial (half-received) copy(ies) found[/bold yellow]\n"
        + "\n".join(lines)
        + "\n\nLeft over by an interrupted receive. They are never used as a base, but they take space "
        "and hide problems.",
        border_style="yellow", title="[yellow]Confirm[/yellow]"))  # fmt: skip
    ap = ask_delete(
        f"Delete {len(deletable)} partial copy(ies) on {dest.label}?",
        [f"{p['kind']}/{p['name']}" for p in deletable],
        print_fn=lambda s: None,
    )
    if ap is None:
        console.print("[dim]Partial copies left in place.[/dim]")
    return ap


def _alert_state(cfg) -> Path:
    return Path(cfg.local_snapshots_dir) / ".last-alert"


def _print_summary(summary: RunSummary, dry_run: bool) -> None:
    for r in summary.results:
        if dry_run:
            style = "green" if r.ok else "red"
            console.print(Panel(
                f"[bold {style}]DRY RUN {'OK' if r.ok else 'FAILED'}[/bold {style}]  [dim]{r.destination}[/dim]\n"
                f"{r.message}", border_style=style))  # fmt: skip
        elif r.status == "completed":
            extra = (
                ("\n[yellow]Warnings:[/yellow] " + "; ".join(r.warnings[:3])) if r.warnings else ""
            )
            pr = summary.pruned.get(r.destination)
            if pr:
                extra += f"\n[dim]Pruned: {', '.join(pr)}[/dim]"
            console.print(Panel(
                f"[bold green]BACKUP COMPLETED[/bold green]  ({r.duration_seconds}s)  [dim]{r.destination}[/dim]\n"
                f"Snapshot: {r.snapshot_name}\n[dim]Run [bold]restore-now[/bold] to browse and recover files.[/dim]" + extra,
                border_style="bold green"))  # fmt: skip
        elif r.status == "partial":
            console.print(Panel(
                f"[bold yellow]BACKUP PARTIAL[/bold yellow]  [dim]{r.destination}[/dim]\n{r.message}\n"
                f"Snapshot kept for inspection: {r.snapshot_name}\n"
                "[dim]'latest' was NOT moved; next run will not use this as a baseline.[/dim]",
                border_style="yellow"))  # fmt: skip
        else:
            console.print(Panel(
                f"[bold red]BACKUP FAILED[/bold red]  [dim]{r.destination}[/dim]\n{r.message}", border_style="red"))  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backup-now",
        description="Incremental backup of $HOME / the system to a USB drive and/or an SSH host.",
    )
    p.add_argument("--dry-run", action="store_true", help="show what would be done, write nothing")
    p.add_argument(
        "--only", choices=("usb", "remote"), help="back up to only this destination kind"
    )
    p.add_argument("--route", choices=("auto", "lan", "wan"), default="auto", help="remote route")
    p.add_argument(
        "--max-disk-percent",
        type=int,
        metavar="PCT",
        help="prune oldest snapshots past this drive %%",
    )
    p.add_argument(
        "--remote-max-gb", type=int, metavar="GB", help="size budget of the remote tree (0 = off)"
    )
    p.add_argument("--keep", type=int, metavar="N", help="hard cap on completed snapshots kept")
    p.add_argument(
        "--min-keep", type=int, metavar="N", help="never prune below this many snapshots"
    )
    p.add_argument("--target", metavar="DIR", help="override the backup drive mount point")
    p.add_argument(
        "--quiet", action="store_true", help="plain line output instead of the live dashboard"
    )
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip the full-backup confirmation (never approves a deletion)")  # fmt: skip
    p.add_argument(
        "--menu", dest="menu", action="store_true", default=None, help="force the launch menu"
    )
    p.add_argument(
        "--no-menu", dest="menu", action="store_false", help="skip the menu even on a terminal"
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    cfg = cfgmod.load()
    if args.max_disk_percent is not None:
        cfg.max_disk_percent = min(99, max(10, args.max_disk_percent))
    if args.remote_max_gb is not None:
        cfg.remote_max_gb = max(0, args.remote_max_gb)
    if args.keep is not None:
        cfg.max_snapshots = max(0, args.keep)
    if args.min_keep is not None:
        cfg.min_keep = max(1, args.min_keep)
    if args.target:
        cfg.target_dir = args.target

    try:
        choice = choose_engine(
            cfg.engine,
            source_btrfs=source_is_btrfs(cfg.source_mounts),
            btrfs_tool=btrfs_tool_present(),
        )
    except EngineError as exc:
        console.print(Panel(f"[bold red]{exc}[/bold red]", border_style="red"))
        return 2
    engine_id = choice.engine
    if not args.dry_run and maybe_elevate(engine_id, argv):
        return 0  # (execvp no vuelve; solo ocurre con un execvp inyectado)

    want = (args.only,) if args.only else ("usb", "remote")
    cands = candidate_destinations(cfg, engine_id, want)
    for d in cands.dests:
        if isinstance(d, SSHDestination) and args.route != "auto":
            d.force_route(args.route)
    console.print(_header(cfg, engine_id, args.dry_run))
    for n in cands.notes:
        console.print(f"[yellow]-[/yellow] {n}")
    if not cands.dests:
        console.print(Panel(
            "[bold red]No backup destination available.[/bold red]\nMount the right drive (it must be a real "
            "mount point), set [bold]TARGET_DIR[/bold], or configure [bold]REMOTE_HOST[/bold] and "
            "[bold]REMOTE_PATH[/bold] in [bold]~/.config/restore-tui/config.conf[/bold].",
            border_style="red"))  # fmt: skip
        return 2

    explicit = any([args.dry_run, args.only, args.yes, args.target, args.route != "auto",
                    args.max_disk_percent is not None, args.keep is not None,
                    args.min_keep is not None, args.remote_max_gb is not None])  # fmt: skip
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    use_menu = args.menu if args.menu is not None else (tty and not explicit)
    targets = None
    if use_menu:
        try:
            from .ui.backup_menu import run_menu
        except Exception:  # noqa: BLE001 - sin textual / sin pantalla
            run_menu = None
        if run_menu is not None:
            picked = run_menu(cfg, cands.dests)
            if not picked:
                console.print("[dim]Cancelled.[/dim]")
                return 0
            args.dry_run = picked["dry_run"]
            cfg.max_disk_percent = picked["max_disk_percent"]
            cfg.remote_max_gb = picked["remote_max_gb"]
            cfg.min_keep = picked["min_keep"]
            targets = picked["destinations"]
    if targets is None:
        targets = []
        for d in cands.dests:
            ok, why = d.reachable()
            if ok:
                console.print(f"[green]+[/green] {d.label}: [dim]{why}[/dim]")
                targets.append(d)
            else:
                console.print(f"[yellow]-[/yellow] {d.label}: skipped [dim]({why})[/dim]")
    if not targets:
        console.print(
            Panel("[bold red]No destination reachable right now.[/bold red]", border_style="red")
        )
        return 2

    engine = make_engine(engine_id, cfg)
    pm, _why = package_manager_here()
    heal_approval = None
    if not args.dry_run:
        kept = []
        for d in targets:
            if confirm_full(d, engine, args.yes):
                kept.append(d)
            else:
                console.print(f"[dim]{d.label} skipped.[/dim]")
        targets = kept
        if not targets:
            console.print("[dim]Nothing to do.[/dim]")
            if not sys.stdin.isatty():
                send_alert("backup-now: destination skipped", "A full backup (or an unreachable destination) "
                           "needs confirmation. Run backup-now in a terminal.", cfg.user, _alert_state(cfg))  # fmt: skip
            return 3
        for d in targets:
            heal_approval = ask_heal(d, engine) or heal_approval  # --yes nunca aprueba un borrado

    runner_kw = dict(pm=manager_for(pm))
    try:
        if args.quiet or args.dry_run:

            def quiet_cb(t, m):
                if t == "progress":
                    console.print(f"  {m}", style="progress", end="\r", highlight=False)
                else:
                    style = t if t in ("info", "warning", "error", "success", "stage") else None
                    console.print(Text(f"[{t}] {m}"), style=style, highlight=False)

            summary = BackupRunner(cfg, engine, targets, emit=quiet_cb, **runner_kw).run(
                args.dry_run, heal_approval
            )
        else:
            from .ui.dashboard import Dashboard
            from rich.live import Live

            dash = Dashboard("Synchronizing")
            runner = BackupRunner(cfg, engine, targets, emit=dash.on_event, **runner_kw)
            stop = threading.Event()

            def refresh(live):
                while not stop.is_set():
                    live.update(dash.render())
                    time.sleep(0.12)

            with Live(dash.render(), console=console, refresh_per_second=8, transient=True) as live:
                rt = threading.Thread(target=refresh, args=(live,), daemon=True)
                rt.start()
                try:
                    summary = runner.run(False, heal_approval)
                finally:
                    stop.set()
                    rt.join(timeout=1.0)
    except KeyboardInterrupt:
        console.print(Panel(
            "[bold yellow]BACKUP CANCELLED[/bold yellow]\nTransfers were stopped. 'latest' was not moved; "
            "the partial snapshot is not used as a baseline.", border_style="yellow"))  # fmt: skip
        return 130

    if not args.dry_run:
        if summary.ok:
            clear_alert(_alert_state(cfg))
        else:
            send_alert(f"backup-now: backup {summary.status.upper()}",
                       summary.message or "See the backup-now output.", cfg.user, _alert_state(cfg))  # fmt: skip
    _print_summary(summary, args.dry_run)
    return 0 if summary.ok else 1


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
