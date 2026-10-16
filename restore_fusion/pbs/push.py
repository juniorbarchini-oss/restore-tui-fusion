"""`restore-tui pbs` / menú [P]: sube a un datastore de Proxmox Backup Server.

Qué se sube lo decide el motor (`pbs_freeze`):
  * btrfs: los snapshots de solo lectura más nuevos `/.snapshots/root_*` y `home_*` -> `root.pxar`,
    `home.pxar`. Necesita root. No lleva `--exclude` (el snapshot ya salió podado).
  * rsync: el `$HOME` VIVO como `home.pxar` (no hay snapshot barato en ext4), con cada exclusión como
    `--exclude`. NO es atómico: puede atrapar un archivo a mitad de escritura; se avisa.
No toma el snapshot ni borra nada: son pasos separados y manuales (no hay cron).
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from rich.console import Console

from .client import CLIENT_HINT, TOOL, PbsClient, PbsError, check_configured, pbs_env

console = Console()

POWER_TOOL = "powerprofilesctl"
POWER_PROFILE = "power-saver"


@dataclass(frozen=True)
class PbsArchive:
    name: str  # "root.pxar" / "home.pxar"
    path: Path
    atomic: bool  # True = snapshot de solo lectura; False = árbol vivo

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.path}"


def latest_snapshot(snapshots_dir: Path, kind: str) -> Optional[Path]:
    """El /.snapshots/{kind}_<ts> más nuevo (el nombre ordena como el tiempo). Ignora archivos y symlinks."""
    snapshots_dir = Path(snapshots_dir)
    if not snapshots_dir.is_dir():
        return None
    found = sorted(p for p in snapshots_dir.glob(f"{kind}_*") if p.is_dir() and not p.is_symlink())
    return found[-1] if found else None


def pbs_freeze(engine_id: str, cfg) -> List[PbsArchive]:
    """Qué sube cada motor. Lanza PbsError si no hay nada que subir."""
    if engine_id == "btrfs":
        out = []
        for kind in ("root", "home"):
            snap = latest_snapshot(Path(cfg.local_snapshots_dir), kind)
            if snap is not None:
                out.append(PbsArchive(f"{kind}.pxar", snap, True))
        if not out:
            raise PbsError(
                f"No snapshots found under {cfg.local_snapshots_dir}. Run Backup now [B] first."
            )
        return out
    home = Path(cfg.home)
    if not home.is_dir():
        raise PbsError(f"{home} not found.")
    return [PbsArchive("home.pxar", home, False)]


def live_exclusions(cfg) -> List[str]:
    from ..engines.rsync_hardlink import exclusions_for

    return exclusions_for(cfg.exclude_defaults, cfg.extra_exclusions)


def power_saver_available(
    which: Callable = shutil.which, runner: Callable = subprocess.run
) -> bool:
    """True si el gestor de energía del sistema ofrece `power-saver`. Si no (sin el servicio, otro
    sistema), el push corre igual y sin cambios: nunca falla por esto."""
    if which(POWER_TOOL) is None:
        return False
    try:
        proc = runner([POWER_TOOL, "list"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and POWER_PROFILE in (proc.stdout or "")


def with_power_saver(argv: Sequence[str]) -> List[str]:
    """Mantiene el perfil `power-saver` MIENTRAS corre `argv` y lo suelta solo al terminar (también
    si el comando muere o se cancela). No cambia el perfil guardado del sistema."""
    return [POWER_TOOL, "launch", "--profile", POWER_PROFILE, "--appid", "restore-fusion",
            "--reason", "Backup to PBS", "--", *argv]  # fmt: skip


def backup_argv(cfg, archives: Sequence[PbsArchive], hostname: str,
                exclusions: Sequence[str] = ()) -> List[str]:  # fmt: skip
    argv = [TOOL, "backup", *[a.spec for a in archives],
            "--repository", cfg.pbs_repository, "--backup-id", hostname]  # fmt: skip
    for pattern in exclusions:
        argv += ["--exclude", pattern]
    return argv


def run_push(
    cfg,
    engine_id: str,
    *,
    geteuid: Callable[[], int] = os.geteuid,
    runner: Callable = subprocess.run,
    which: Callable = shutil.which,
    hostname: Optional[str] = None,
    out: Console = console,
) -> int:
    if engine_id == "btrfs" and geteuid() != 0:
        out.print("[red]This needs root (reading root-owned files under /.snapshots).[/]")
        return 1
    help_text = check_configured(cfg)
    if help_text:
        out.print(f"[yellow]{help_text}[/]", markup=True)
        return 1
    if which(TOOL) is None:
        out.print(f"[red]{TOOL} not found.[/] {CLIENT_HINT}")
        return 1
    try:
        archives = pbs_freeze(engine_id, cfg)
    except PbsError as exc:
        out.print(f"[yellow]{exc}[/]")
        return 1

    live = [a for a in archives if not a.atomic]
    exclusions = live_exclusions(cfg) if live else []
    for a in archives:
        out.print(f"[#00FF66]{a.name.split('.')[0]} -> {a.path}[/]")
    if live:
        out.print(
            "[yellow]Live copy of $HOME (not a snapshot): a file being written right now may be "
            "caught half-written.[/]"
        )
        out.print(f"[dim #00FF66]{len(exclusions)} exclusion pattern(s) applied[/]")
    out.print(f"[dim #00FF66]-> {cfg.pbs_repository}[/]\n")

    argv = backup_argv(cfg, archives, hostname or os.uname().nodename, exclusions)
    if cfg.pbs_power_saver and power_saver_available(which):
        argv = with_power_saver(argv)
        out.print("[dim #00FF66]Power profile: power-saver while this runs[/]\n")
    try:
        proc = runner(argv, env=pbs_env(cfg))
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        out.print(f"[red]{TOOL} not found on PATH.[/] {CLIENT_HINT}")
        return 1
    if proc.returncode == 0:
        out.print("\n[bold #00FF66]Backup to PBS complete.[/]")
    else:
        out.print(f"\n[red]{TOOL} exited with code {proc.returncode}.[/]")
    return proc.returncode


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
    if maybe_elevate(engine_id, argv, module="restore_fusion.pbs.push"):
        return 0
    return run_push(cfg, engine_id)


if __name__ == "__main__":
    sys.exit(main())
