"""`restore-tui --paths` y `restore-tui --gc`.

--paths  lista cada lugar donde la herramienta crea o toca algo (para auditar o desinstalar).
--gc     quita lo que dejaron corridas MUERTAS (staging btrfs, scratch de backup, montajes sshfs).
         Solo toca directorios con nombre `<pid>-...` cuyo pid ya no existe; una corrida viva y
         todo lo demás (snapshots, backups, config) quedan intactos. No borra copias de seguridad.
"""

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from rich.console import Console

from . import config as cfgmod

console = Console()

Row = Tuple[str, str]


def paths_report(cfg, home: Path) -> List[Row]:
    """(ruta, nota). Una fuente de verdad que el instalador y el desinstalador también nombran."""
    snaps = Path(cfg.local_snapshots_dir)
    target = str(cfg.target_dir) if cfg.target_dir else "<USB>/{restore-tui,btrfs-restore}/"
    remote = (
        f"{cfg.remote_host}:{cfg.remote_path}"
        if cfg.remote_host and cfg.remote_path
        else "<remote>"
    )
    return [
        ("/opt/restore-fusion/", "installed app + venv (install.sh)"),
        ("/usr/local/bin/{restore-tui,restore-now,backup-now}", "command symlinks (install.sh)"),
        ("/usr/share/applications/restore-fusion.desktop", "desktop entry (install.sh)"),
        (
            str(home / ".config/restore-tui/config.conf"),
            "user config, mode 600 - never auto-removed",
        ),
        (
            str(home / ".config/btrfs-restore/config.conf"),
            "old btrfs-edition config, read if present - never touched",
        ),
        (
            str(home / ".local/state/restore-tui/welcome-shown"),
            "marker: the welcome screen was shown (safe to delete)",
        ),
        (
            "/opt/restore-fusion-archive/",
            "old installs retired by install.sh (archived, never deleted)",
        ),
        (f"{snaps}/{{root,home}}_<ts>", "local read-only snapshots (btrfs engine)"),
        (f"{snaps}/.backup-tmp/<pid>-<ts>/", "transient backup state - swept on start / by --gc"),
        (f"{snaps}/.backup-last.log", "last backup log (single file, overwritten)"),
        (f"{snaps}/staging/<pid>-<slug>/", "restore staging subvolume - swept on start / by --gc"),
        (f"{snaps}/.remote-mnt/<pid>-<slug>/", "legacy read-only SSHFS mount dir (old edition)"),
        (
            "$TMPDIR/restore-fusion-mnt-<uid>/<pid>-<slug>/",
            "read-only SSHFS mount of a remote snapshot - unmounted on exit / by --gc",
        ),
        (
            "$TMPDIR/pbs-restore-<pid>-*/",
            "read-only FUSE mounts of a PBS snapshot - unmounted on exit",
        ),
        (target, "backups on the USB / target drive"),
        (f"{remote}/{{root,home,meta}}/ or restore-tui/", "backups on the SSH host"),
    ]


def run_paths(cfg, home: Path, out: Console = console) -> int:
    rows = paths_report(cfg, home)
    w = max(len(p) for p, _ in rows)
    out.print("\n[bold #00FF66]Paths this tool creates or modifies[/]\n")
    for p, note in rows:
        out.print(f"  {p.ljust(w)}  {note}", markup=False, highlight=False)
    out.print()
    return 0


def _free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free if path.exists() else 0
    except OSError:
        return 0


def run_gc(cfg, *, staging=None, scratch=None, mounter=None, out: Console = console) -> int:
    from .backup.scratch import Scratch
    from .engines.btrfs.staging import Staging
    from .restore.remote_mount import RemoteMounter

    probe = Path(cfg.local_snapshots_dir)
    before = _free(probe)
    staging = staging or Staging(cfg)
    scratch = scratch or Scratch(probe)
    mounter = mounter or RemoteMounter()
    swept = list(scratch.sweep_stale())
    staging.sweep_orphans()
    mounter.sweep_orphans()
    freed = _free(probe) - before
    if swept or freed > 1 << 20:
        out.print(
            f"[#00FF66]swept {len(swept)} scratch dir(s), freed ~{max(freed, 0) / (1 << 30):.2f} GiB[/]"
        )
    else:
        out.print("[#00FF66]nothing to collect - no dead-run leftovers[/]")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = cfgmod.load()
    if args and args[0] == "--paths":
        return run_paths(cfg, Path(cfg.home))
    if args and args[0] == "--gc":
        # el staging y el scratch viven bajo /.snapshots (btrfs): ahí hace falta root
        if Path(cfg.local_snapshots_dir).exists() and os.geteuid() != 0:
            from .cli_backup import maybe_elevate

            if maybe_elevate("btrfs", args, module="restore_fusion.maintenance"):
                return 0
        return run_gc(cfg)
    console.print("usage: restore-tui --paths | --gc")
    return 2


if __name__ == "__main__":
    sys.exit(main())
