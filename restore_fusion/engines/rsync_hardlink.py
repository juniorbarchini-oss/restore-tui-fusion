"""Motor rsync + hardlinks POSIX (`--link-dest`), de restore-tui/backup.py.

Cada corrida crea  <root>/snapshots/<YYYY-MM-DD_HHMMSS>/  con:
    home/            copia completa de $HOME (lo que no cambió, hardlink al baseline)
    _system_state/   lo que prepare el runner (ver system_state/)  + restore.sh
    manifest.json    estado máquina-legible (v2, con "engine": "rsync")
    backup.log       bitácora de la corrida

Reglas (README):
  * Una corrida que rsync no terminó limpia es partial/failed: NUNCA es baseline ni mueve
    `latest` (model/manifest.py decide el baseline: solo completed del mismo motor).
  * rsync 24 (archivos desaparecieron) y 25 son éxito con aviso: normal en un $HOME vivo.
  * `--numeric-ids` para conservar dueños al restaurar en un sistema recién instalado.
  * Un Ctrl-C/SIGTERM mata rsync y deja el snapshot como `partial` (no `running`).
  * La poda NO ocurre aquí: la hace el runner con plan_prune + Approval.
"""

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..destinations.base import Destination, RemoteQueryError
from ..model.manifest import (
    COMPLETED,
    FAILED,
    PARTIAL,
    RUNNING,
    SnapshotRecord,
    new_manifest,
    pick_baseline,
)
from .base import CopyEngine, EngineContext, EngineResult, Emit, FullCheck, no_emit

# rsync sale con estos códigos cuando "terminó, pero ojo"
RSYNC_WARN_OK = {24, 25}
MAX_DIAG_LINES = 20  # líneas de diagnóstico de rsync que se conservan
SHOWN_DIAG_LINES = 3  # las que se citan en el mensaje de error

# Rutas (relativas a $HOME) que nunca vale la pena respaldar: cachés, basura, blobs
# re-descargables, otros backups e iconos/temas GTK descargados (granjas de symlinks).
# ~/Documents/desarrollo/ NO se excluye: guarda trabajo sin commitear.
DEFAULT_EXCLUSIONS = [
    "GoogleDrive/",
    "Backups/",
    "backup_ob_vault_*/",
    "*.tar.gz",
    "*.tar.zst",
    "snap/steam/",
    ".steam/",
    ".local/share/Steam/",
    ".ollama/models/",
    "**/.ollama/models/",
    ".cache/",
    "**/.cache/",
    ".local/share/Trash/",
    ".local/share/virtualenvs/",
    "**/.venv/",
    "**/__pycache__/",
    "**/.pytest_cache/",
    "**/node_modules/",
    "**/.cargo/registry/cache/",
    ".mozilla/firefox/*default*/cache2/",
    ".config/google-chrome/*/Service Worker/CacheStorage/",
    ".config/google-chrome/*/Application Cache/",
    ".config/Code/Cache/",
    ".config/Code/CachedData/",
    ".var/app/*/cache/",
    "**/.var/app/*/cache/",
    ".thumbnails/",
    ".local/share/baloo/",
    ".icons/",
    ".themes/",
    ".local/share/icons/",
    ".local/share/themes/",
]


def exclusions_for(exclude_defaults: bool, extra: List[str]) -> List[str]:
    """Lista integrada (si no se apagó) + las EXCLUDE del usuario, sin duplicados, en orden."""
    pats = (list(DEFAULT_EXCLUSIONS) if exclude_defaults else []) + list(extra)
    seen = set()
    return [p for p in pats if not (p in seen or seen.add(p))]


def rsync_common(exclusions: List[str], dry_run: bool = False) -> List[str]:
    # En un dry run NO se pasan -A/-X: el destino aún no existe y rsync >= 3.4 intenta leer sus xattrs
    # (`gen_entry_copy_xattrs: openat ... No such file`, exit 23). Un dry run solo necesita la lista.
    cmd = [
        "rsync", "-a" if dry_run else "-aAX",
        "--numeric-ids",
        "-x",  # no cruzar fronteras de filesystem
        "--delete", "--delete-excluded",
        "--no-inc-recursive",  # total exacto en --info=progress2
        "--info=progress2,stats2",
    ]  # fmt: skip
    for pattern in exclusions:
        cmd += ["--exclude", pattern]
    return cmd


class RsyncHardlinkEngine(CopyEngine):
    id = "rsync"

    def __init__(self, rsync_bin: str = "rsync"):
        self.rsync_bin = rsync_bin
        self.last_diagnostics: List[str] = []

    def _why(self) -> str:
        """Primeras líneas que rsync escribió, para que un exit 23/11 diga POR QUÉ, no solo el código."""
        lines = self.last_diagnostics[:SHOWN_DIAG_LINES]
        return (": " + " | ".join(lines)) if lines else " (rsync printed no diagnostics)"

    def available(self) -> Tuple[bool, str]:
        if not shutil.which(self.rsync_bin):
            return False, "rsync is not installed"
        return True, "ok"

    # -- historia ------------------------------------------------------------------
    def records(self, dest: Destination) -> List[SnapshotRecord]:
        """Raises RemoteQueryError si el destino no se puede consultar."""
        out = []
        for name in dest.list_snapshots():
            out.append(
                SnapshotRecord(name, dest.read_manifest(name), tuple(dest.list_entries(name)))
            )
        return out

    def baseline(self, dest: Destination) -> Optional[SnapshotRecord]:
        return pick_baseline(self.records(dest), self.id)

    def would_be_full(self, dest: Destination) -> FullCheck:
        try:
            return FullCheck.YES if self.baseline(dest) is None else FullCheck.NO
        except (RemoteQueryError, OSError):
            return FullCheck.UNKNOWN

    # -- corrida ------------------------------------------------------------------------
    def run(
        self, dest: Destination, ctx: EngineContext, emit: Emit = no_emit, dry_run: bool = False
    ) -> EngineResult:
        result = EngineResult(status=FAILED, destination=dest.label)
        ok, why = dest.reachable()
        if not ok:
            result.message = f"{dest.label}: not reachable ({why})"
            return result
        ok, why = self.available()
        if not ok:
            result.message = why
            return result

        try:
            base_rec = self.baseline(dest)
        except (RemoteQueryError, OSError) as exc:
            # no se pudo preguntar qué hay: NO se asume "vacío" (sería un backup completo a ciegas)
            result.message = (
                f"{dest.label}: cannot read the existing snapshots ({exc}); not sending"
            )
            return result
        baseline = base_rec.name if base_rec else None
        result.baseline = baseline

        if dry_run:
            return self._dry_run(dest, ctx, emit, result)

        ok, why = dest.ensure_ready()
        if not ok:
            result.message = f"{dest.label}: {why}"
            return result

        start = time.time()
        name = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        try:
            name = dest.reserve_snapshot(name)
        except OSError as exc:
            result.message = f"cannot create snapshot dir: {exc}"
            return result
        result.snapshot_name = name
        result.snapshot_path = dest.snapshot_fs_path(name)

        log: List[str] = []

        def say(kind: str, msg: str) -> None:
            log.append(f"[{datetime.now():%H:%M:%S}] {kind.upper():8} {msg}")
            emit(kind, msg)

        def manifest(status: str, **fields) -> dict:
            return new_manifest(
                "rsync", name, status,
                hostname=ctx.hostname, user=ctx.user, source_home=str(ctx.source_home),
                destination=dest.label, link_dest_baseline=baseline, **fields,
            )  # fmt: skip

        try:
            dest.write_manifest(name, manifest(RUNNING, started_at=datetime.now().isoformat()))
            say(
                "info",
                (
                    f"baseline: {baseline} (rsync --link-dest)"
                    if baseline
                    else "no completed snapshot yet - full initial backup"
                ),
            )
            say("stage", f"Syncing $HOME -> {dest.label}")
            rsync_exit = self._rsync(dest, ctx, name, baseline, False, say)
            duration = round(time.time() - start, 2)
            result.duration_seconds = duration
            result.details["rsync_exit"] = rsync_exit

            if rsync_exit == 0 or rsync_exit in RSYNC_WARN_OK:
                result.status = COMPLETED
                if rsync_exit in RSYNC_WARN_OK:
                    msg = f"rsync exit {rsync_exit} (normal on a live home) - snapshot is valid"
                    say("warning", msg)
                    result.warnings.append(msg)
            elif rsync_exit == 23:
                result.status = PARTIAL
                result.message = f"rsync exit 23: some files could not be transferred{self._why()}"
            else:
                result.status = FAILED
                result.message = f"rsync failed with exit code {rsync_exit}{self._why()}"
            self._finish(dest, ctx, name, result, manifest, log, say)
        except BaseException:
            # Ctrl-C / SIGTERM: el snapshot queda `partial`, `latest` no se mueve
            try:
                dest.write_manifest(
                    name,
                    manifest(PARTIAL, failed_at=datetime.now().isoformat(), error="interrupted"),
                )
            except (OSError, RemoteQueryError):
                pass
            raise
        return result

    def _finish(self, dest, ctx, name, result, manifest, log, say) -> None:
        if ctx.stage_dir is not None:
            try:
                (Path(ctx.stage_dir) / "backup.log").write_text("\n".join(log) + "\n")
                dest.push_meta(Path(ctx.stage_dir), name)
            except OSError as exc:
                say("warning", f"could not push snapshot metadata: {exc}")
        final = manifest(
            result.status,
            created_at=datetime.now().isoformat(),
            duration_seconds=result.duration_seconds,
            rsync_exit=result.details.get("rsync_exit"),
            warnings=result.warnings,
        )
        try:  # manifiesto autoritativo (el push de la metadata pudo ser parcial)
            dest.write_manifest(name, final)
        except OSError as exc:
            say("warning", f"could not write manifest: {exc}")
            if result.status == COMPLETED:
                # sin manifest "completed" no se puede mover latest ni usar de baseline
                result.status = PARTIAL
                result.message = f"manifest could not be written: {exc}"
        if result.status == COMPLETED:
            try:
                dest.update_latest(name)
                say("success", f"Snapshot completed in {result.duration_seconds}s: {name}")
            except (OSError, ValueError) as exc:
                say("warning", f"could not update 'latest': {exc}")
        else:
            say("error", result.message)
            say("info", "latest pointer left untouched; this snapshot will not be a baseline")

    # -- rsync -------------------------------------------------------------------------------
    def _argv(self, dest: Destination, ctx: EngineContext, name: str, baseline, dry_run: bool):
        cmd = rsync_common(ctx.exclusions, dry_run)
        cmd[0] = self.rsync_bin
        if dry_run:
            cmd.append("-n")
        cmd += dest.rsync_transport()
        if baseline:
            cmd.append(f"--link-dest={dest.tree_path(baseline)}/home")
        cmd += [f"{ctx.source_home}/", dest.spec(f"{dest.tree_path(name)}/home/")]
        return cmd

    def _rsync(self, dest, ctx, name, baseline, dry_run: bool, say) -> int:
        if not dry_run:
            home_dir = dest.snapshot_fs_path(name)
            if home_dir is not None:  # destino local: rsync solo crea el último componente
                (home_dir / "home").mkdir(parents=True, exist_ok=True)
        cmd = self._argv(dest, ctx, name, baseline, dry_run)
        say("info", "rsync " + " ".join(cmd[1:7]) + " ...")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        last = 0.0
        self.last_diagnostics = []
        try:
            for line in iter(proc.stdout.readline, ""):
                text = line.strip()
                if not text:
                    continue
                if (
                    len(self.last_diagnostics) < MAX_DIAG_LINES
                    and not ("%" in text and ("to-chk=" in text or "xfr#" in text))
                    and not text.startswith(("Number of", "Total ", "sent ", "total size"))
                    and ":" in text
                ):
                    self.last_diagnostics.append(text)  # lo que rsync dijo por stderr (mezclado)
                if "%" in text and ("to-chk=" in text or "xfr#" in text):
                    now = time.time()
                    if now - last >= 0.7:
                        say("progress", text)
                        last = now
                elif text.startswith(("Number of", "Total ", "sent ", "total size")):
                    say("info", text)
                elif "rsync error" in text or "rsync: " in text:
                    say("warning", text)
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        return proc.returncode

    def _dry_run(self, dest, ctx, emit, result) -> EngineResult:
        emit("stage", f"DRY RUN ({dest.label}) - nothing will be written")
        name = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        emit("info", f"would create snapshot: {name}")
        emit("info", f"baseline: {result.baseline or '(none - full backup)'}")
        code = self._rsync(dest, ctx, name, result.baseline, True, emit)
        result.snapshot_name = name
        result.details["rsync_exit"] = code
        ok = code == 0 or code in RSYNC_WARN_OK
        result.status = COMPLETED if ok else FAILED
        result.details["rsync_diagnostics"] = list(self.last_diagnostics)
        result.message = "dry run finished" if ok else f"dry run rsync exit {code}{self._why()}"
        return result


def read_manifest_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
