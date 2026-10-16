"""Motor btrfs: snapshots de solo lectura + `btrfs send | receive` a una unidad USB o a un host SSH.

De btrfs-restore-tui/backup.py. Dos destinos, dos mecanismos de "¿está completo?", UNA interfaz
de baseline (engines/baseline.py):

  * USB (LocalDestination)  : layout con manifest.json -> ManifestBaselineSource (M4).
  * Remoto (SSHDestination) : árbol <base>/{root,home,meta} SIN manifest -> el mecanismo de btrfs
                              (received_uuid), ReceivedUuidBaselineSource. No se inventan manifests
                              para el remoto ni se exigen para leer backups existentes.

Reglas (README):
  * Una corrida que no termina limpia es partial/failed: `latest` no se mueve y esa copia nunca
    es padre incremental. Un Ctrl-C/SIGTERM a mitad de un envío deja el snapshot `partial`.
  * Una consulta remota que falla JAMÁS se lee como "vacío" (no se cae a un envío completo).
  * Este motor NO borra snapshots ni copias remotas por su cuenta: solo planea
    (`plan_*`) y ejecuta (`prune_*`, `heal`) con un Approval. Único borrado propio: el scratch
    transitorio por pid (backup/scratch.py).
  * Necesita root (snapshot/send). Un snapshot local con el mismo nombre ya existente aborta la
    corrida (el original lo borraba sin preguntar).

El recovery kit y la poda automática los orquesta el runner (M11); aquí solo hay planes.
"""

import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ...backup.scratch import Scratch
from ...config.schema import Config
from ...destinations.base import RemoteQueryError
from ...destinations.local import LocalDestination
from ...destinations.ssh import SSHDestination, remote_fs_message, remote_fs_type
from ...drives import fstype_of
from ...model.manifest import COMPLETED, FAILED, PARTIAL, RUNNING, SnapshotRecord, new_manifest
from ...safety.approval import Approval, apply_deletions
from ...safety.retention_plan import PrunePlan, RetentionPolicy, plan_prune
from ..base import CopyEngine, EngineContext, EngineResult, Emit, FullCheck, no_emit
from ..baseline import Baseline, BaselineSource, ManifestBaselineSource
from .exclusions import exclusions_for
from .ops import BtrfsOps, CommandError
from .remote import COMPACT_RE, ReceivedUuidBaselineSource, RemoteBtrfs, item_id


def _raise_on_sigterm(signum, _frame):
    # SystemExit sí corre los `finally` que limpian el scratch (la disposición por defecto no)
    raise SystemExit(128 + signum)


def kind_of(mount: str) -> str:
    return "root" if mount == "/" else Path(mount).name


class BtrfsSendEngine(CopyEngine):
    id = "btrfs"

    def __init__(
        self,
        cfg: Config,
        ops: Optional[BtrfsOps] = None,
        *,
        is_root: Callable[[], bool] = lambda: os.geteuid() == 0,
    ):
        self.cfg = cfg
        self._is_root = is_root
        self._send_label = ""
        self._emit_cb: Emit = no_emit
        self._fstype = fstype_of  # inyectable en tests
        self.ops = ops or BtrfsOps(progress_cb=self._on_progress)

    def _on_progress(self, text: str) -> None:
        self._emit_cb("progress", f"{self._send_label}{text}")

    # -- disponibilidad ------------------------------------------------------------
    def available(self):
        if not shutil.which("btrfs"):
            return False, "btrfs-progs is not installed"
        if not self._is_root():
            return False, "backup needs root (btrfs snapshot/send)"
        return True, "ok"

    @property
    def kinds(self) -> List[str]:
        return [kind_of(m) for m in self.cfg.source_mounts]

    @property
    def local_dir(self) -> Path:
        return Path(self.cfg.local_snapshots_dir)

    # -- historia / baseline --------------------------------------------------------------
    def records(self, dest: LocalDestination) -> List[SnapshotRecord]:
        return [
            SnapshotRecord(n, dest.read_manifest(n), tuple(dest.list_entries(n)))
            for n in dest.list_snapshots()
        ]

    def _local_parent(self, kind: str, rec: SnapshotRecord) -> Optional[Path]:
        """El snapshot LOCAL que registró el último backup completado para `kind`, si sigue
        existiendo: la única base segura para `btrfs send -p`."""
        name = (rec.manifest.get("local_snapshots") or {}).get(kind)
        if not name:
            return None
        p = self.local_dir / name
        return p if p.is_dir() else None

    def remote_for(self, dest: SSHDestination) -> RemoteBtrfs:
        ssh = dest.ssh_argv()
        return RemoteBtrfs(
            self.ops,
            ssh[:-1],
            ssh[-1],
            dest.root,
            self.kinds,
            self.local_dir,
            self.cfg.effective_remote_keep,
        )

    def baseline_source(self, dest) -> BaselineSource:
        """La interfaz común: manifests en el USB, received_uuid en el remoto btrfs."""
        if isinstance(dest, SSHDestination):
            return ReceivedUuidBaselineSource(self.remote_for(dest))
        return ManifestBaselineSource(self.records(dest), "btrfs", self._local_parent)

    def full_kinds(self, dest) -> List[str]:
        """Kinds cuyo envío sería COMPLETO. RemoteQueryError si no se pudo preguntar."""
        return self.baseline_source(dest).would_be_full(self.kinds)

    def would_be_full(self, dest) -> FullCheck:
        try:
            return FullCheck.YES if self.full_kinds(dest) else FullCheck.NO
        except (RemoteQueryError, OSError, subprocess.SubprocessError):
            return FullCheck.UNKNOWN

    def partial_copies(self, dest: SSHDestination) -> List[dict]:
        return self.remote_for(dest).partial_copies()

    # -- corrida -----------------------------------------------------------------------------
    def run(
        self, dest, ctx: EngineContext, emit: Emit = no_emit, dry_run: bool = False
    ) -> EngineResult:
        return self.run_many([dest], ctx, emit, dry_run)[0]

    def run_many(
        self,
        dests: Sequence,
        ctx: EngineContext,
        emit: Emit = no_emit,
        dry_run: bool = False,
        heal_approval: Optional[Approval] = None,
    ) -> List[EngineResult]:
        """Un solo juego de snapshots locales RO, enviado a cada destino. Un resultado por destino."""
        self._emit_cb = emit
        name = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        compact = name.replace("-", "")
        start = time.time()
        results = [
            EngineResult(status=FAILED, destination=d.label, snapshot_name=name) for d in dests
        ]

        active = []
        for d, r in zip(dests, results):
            if isinstance(d, LocalDestination):
                ok, why = d.reachable()
                if not ok:
                    r.message = f"{d.label}: {why}"
                    continue
            active.append((d, r))

        if dry_run:
            return self._dry_run(name, active, results, emit)
        if not active:
            return results
        if not self._is_root():
            for _d, r in active:
                r.message = "backup needs root (btrfs snapshot/send)"
            return results

        scratch = Scratch(self.local_dir, emit)
        try:
            self.local_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        scratch.sweep_stale()
        try:
            run_dir = scratch.new_run_dir(compact)
        except OSError as exc:
            for _d, r in active:
                r.message = f"cannot create the scratch dir: {exc}"
            return results
        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _raise_on_sigterm)
        except (ValueError, OSError):
            prev_sigterm = None  # no es el hilo principal
        logf = open(run_dir / "backup.log", "w", encoding="utf-8")

        def say(kind: str, msg: str) -> None:
            try:
                logf.write(f"[{datetime.now():%H:%M:%S}] {kind.upper():8} {msg}\n")
                logf.flush()
            except (OSError, ValueError):
                pass
            emit(kind, msg)

        self._emit_cb = say
        try:
            local_snaps: Dict[str, str] = {}
            excluded: Dict[str, List[str]] = {}
            try:
                for mount in self.cfg.source_mounts:
                    kind = kind_of(mount)
                    say("stage", f"Snapshot {mount}")
                    local = self.local_dir / f"{kind}_{compact}"
                    dropped = self._make_local_snapshot(
                        Path(mount),
                        local,
                        exclusions_for(mount, self.cfg.exclude_defaults, self.cfg.extra_exclusions),
                    )
                    local_snaps[kind] = local.name
                    if dropped:
                        excluded[kind] = dropped
                        say(
                            "info",
                            f"{kind}: left out {len(dropped)} cache/trash path(s): "
                            f"{', '.join(dropped)}",
                        )
            except (CommandError, OSError, RuntimeError) as exc:
                for _d, r in active:
                    r.message = f"local snapshot failed: {exc}"
                    say("error", r.message)
                return results

            for d, r in active:
                r.details["local_snapshots"] = dict(local_snaps)
                r.details["excluded"] = excluded
                if isinstance(d, SSHDestination):
                    self._to_remote(d, r, name, local_snaps, ctx, say, heal_approval)
                else:
                    self._to_usb(d, r, name, local_snaps, excluded, ctx, say)
                r.duration_seconds = round(time.time() - start, 2)
        finally:
            if prev_sigterm is not None:
                try:
                    signal.signal(signal.SIGTERM, prev_sigterm)
                except (ValueError, OSError):
                    pass
            try:
                logf.close()
            except OSError:
                pass
            scratch.keep_last_log(run_dir)  # solo la bitácora de la última corrida
            scratch.cleanup(run_dir)
            self._emit_cb = emit
        return results

    def _remote_fs_notice(self, dest: SSHDestination) -> str:
        """Aviso (no bloquea, nunca cambia el motor): `btrfs receive` solo funciona sobre btrfs.
        Si la consulta falla se dice y se sigue como antes. '' = btrfs, nada que decir."""
        fs = remote_fs_type(dest, self.ops.ssh_capture)
        return remote_fs_message(dest.label, dest.root, fs)

    def _stream_notice(self, dest: LocalDestination) -> str:
        """Aviso (no bloquea): un destino local que no es btrfs guarda streams `.btrfs.zst`."""
        fs = self._fstype(dest.mount)
        what = f"is {fs}, not btrfs" if fs else "is not btrfs"
        return (
            f"{dest.mount} {what}: snapshots are saved as .btrfs.zst stream files. "
            "Restoring them needs a staging step on a btrfs location (see STAGING_DIR)."
        )

    # -- USB ---------------------------------------------------------------------------------------
    def _to_usb(
        self, dest: LocalDestination, r: EngineResult, name, local_snaps, excluded, ctx, say
    ):
        ok, why = dest.ensure_ready()
        if not ok:
            r.message = f"{dest.label}: {why}"
            say("error", r.message)
            return
        try:
            records = self.records(dest)
            snap = dest.reserve_snapshot(name)
        except OSError as exc:
            r.message = f"{dest.label}: cannot create snapshot dir: {exc}"
            say("error", r.message)
            return
        r.snapshot_name, r.snapshot_path = snap, dest.snapshot_fs_path(snap)
        source = ManifestBaselineSource(records, "btrfs", self._local_parent)
        target_is_btrfs = self.ops.is_btrfs(dest.mount)
        if not target_is_btrfs:
            say("warning", f"{dest.label}: {self._stream_notice(dest)}")
        base_fields = {"hostname": ctx.hostname, "user": ctx.user}
        dest.write_manifest(
            snap,
            new_manifest(
                "btrfs", snap, RUNNING, started_at=datetime.now().isoformat(), **base_fields
            ),
        )
        try:
            # el system state se COPIA, nunca se mueve: el remoto reusa el mismo directorio
            if ctx.stage_dir is not None:
                for item in Path(ctx.stage_dir).iterdir():
                    target = r.snapshot_path / item.name
                    if item.is_dir():
                        shutil.copytree(item, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, target)
            parents: Dict[str, Optional[str]] = {}
            for kind, local_name in local_snaps.items():
                local = self.local_dir / local_name
                b = source.baseline(kind)
                parents[kind] = b.name if b else None
                say("stage", f"{dest.label}: send {kind} ({'incremental' if b else 'full'})")
                self._send_usb(
                    kind, local, b.local_path if b else None, r.snapshot_path, target_is_btrfs
                )
            dest.write_manifest(
                snap,
                new_manifest(
                    "btrfs", snap, COMPLETED, created_at=datetime.now().isoformat(),
                    target_is_btrfs=bool(target_is_btrfs), local_snapshots=local_snaps,
                    parents=parents, excluded=excluded, warnings=r.warnings, **base_fields,
                ),
            )  # fmt: skip
            r.details["parents"] = parents
            r.baseline = None
            r.status = COMPLETED
            try:
                dest.update_latest(snap)
            except (OSError, ValueError) as exc:
                say("warning", f"could not update 'latest': {exc}")
        except (CommandError, OSError, RuntimeError) as exc:
            say("error", f"{dest.label}: {exc}")
            self._mark_partial(dest, snap, str(exc), base_fields)
            r.status = PARTIAL if isinstance(exc, CommandError) else FAILED
            r.message = str(exc)
        except BaseException:
            # Ctrl-C / SIGTERM a mitad de un envío: partial => `latest` no se mueve y esta copia
            # nunca será padre; la interrupción sigue su camino
            say("error", f"{dest.label}: interrupted")
            self._mark_partial(dest, snap, "interrupted", base_fields)
            raise

    def _mark_partial(self, dest, snap, error, base_fields) -> None:
        try:
            dest.write_manifest(
                snap,
                new_manifest("btrfs", snap, PARTIAL, failed_at=datetime.now().isoformat(),
                             error=error, **base_fields),
            )  # fmt: skip
        except OSError:
            pass

    def _send_usb(self, kind, local: Path, parent: Optional[Path], snap_dir: Path, target_is_btrfs):
        self._send_label = f"USB {kind} "
        if target_is_btrfs:
            rc = self.ops.send_local_receive(local, parent, snap_dir)
        else:
            rc = self.ops.send_to_stream(local, parent, snap_dir / f"{kind}.btrfs.zst")
        if rc != 0:
            raise CommandError(rc, f"btrfs send {kind}")

    # -- remoto ------------------------------------------------------------------------------------------
    def _to_remote(
        self, dest: SSHDestination, r: EngineResult, name, local_snaps, ctx, say, heal_approval
    ):
        remote = self.remote_for(dest)
        label = dest.label
        notice = self._remote_fs_notice(dest)
        if notice:
            say("warning", notice)
        try:
            ok, why = remote.ensure_dirs()
            if not ok:
                r.message = f"{label}: {why}"
                say("error", r.message)
                return
            remote.heal(heal_approval, local_snaps, say)
            parents = {}
            for kind, local_name in local_snaps.items():
                local = self.local_dir / local_name
                b = remote.baseline(kind)
                say("stage", f"{label}: send {kind} ({'incremental' if b else 'full'})")
                self._send_label = f"{label} {kind} "
                rc = remote.send(kind, local, b.local_path if b else None)
                if rc != 0:
                    raise CommandError(
                        rc,
                        f"send {kind} to {label}",
                        stderr=getattr(self.ops, "last_stderr", "") or None,
                    )
                parents[f"{label}/{kind}"] = b.name if b else None
            r.details["parents"] = parents
            say("stage", f"{label}: system state")
            if ctx.stage_dir is not None:
                try:
                    rc, err = remote.push_state(name, Path(ctx.stage_dir))
                    if rc != 0:
                        say("warning", f"{label}: system state not pushed: {err or rc}")
                except (OSError, subprocess.SubprocessError) as exc:
                    say("warning", f"{label}: system state not pushed: {exc}")
            r.status = COMPLETED
        except CommandError as exc:
            detail = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
            r.status, r.message = PARTIAL, detail or str(exc)
            say("error", f"{label}: {exc}" + (f" - {detail}" if detail else ""))
        except RemoteQueryError as exc:
            r.status, r.message = FAILED, str(exc)
            say(
                "error",
                f"{label}: {exc} - not sending, a blind full backup could hide a network problem",
            )
        except (OSError, RuntimeError) as exc:
            r.status, r.message = FAILED, str(exc)
            say("error", f"{label}: {exc}")

    # -- pasos -----------------------------------------------------------------------------------------------
    def _make_local_snapshot(
        self, mount: Path, dest: Path, exclusions: Optional[List[str]] = None
    ) -> List[str]:
        """Snapshot RO de `mount` en `dest`. Con exclusiones: se toma ESCRIBIBLE, se borran las
        rutas de caché/basura DENTRO DEL SNAPSHOT y se pasa a solo lectura antes de usarlo como
        origen de `send` o padre incremental. Devuelve lo que se borró."""
        if dest.exists():
            raise OSError(f"local snapshot {dest.name} already exists; not replacing it")
        if exclusions:
            self.ops.snapshot_rw(mount, dest)
            dropped = self.ops.prune_paths(dest, exclusions)
            self.ops.set_readonly(dest)
            return dropped
        self.ops.snapshot_ro(mount, dest)
        return []

    def _dry_run(self, name, active, results, emit) -> List[EngineResult]:
        emit("stage", "DRY RUN - nothing will be written")
        emit("info", f"would create snapshot: {name}")
        if not active:
            return results
        for d, r in active:
            if isinstance(d, SSHDestination):
                emit("info", f"target: {d.label} {d.user}@{d.host}:{d.root}")
                notice = self._remote_fs_notice(d)
                if notice:
                    emit("warning", notice)
            else:
                tb = self.ops.is_btrfs(d.mount)
                emit(
                    "info",
                    f"target: {d.label} {d.root} "
                    f"({'btrfs send/receive' if tb else '.btrfs.zst streams'})",
                )
                if not tb:
                    emit("warning", f"{d.label}: {self._stream_notice(d)}")
            r.status, r.message = COMPLETED, "dry run finished"
        emit(
            "info",
            f"sources: {' '.join(self.cfg.source_mounts)} "
            "(incremental per target when a shared parent exists)",
        )
        for m in self.cfg.source_mounts:
            ex = exclusions_for(m, self.cfg.exclude_defaults, self.cfg.extra_exclusions)
            if ex:
                emit("info", f"{m}: would leave out {len(ex)} cache/trash glob(s)")
        return results

    def write_remote_kit(self, dest: SSHDestination, files: Dict[str, bytes]) -> None:
        """Deja el kit de recuperación en <base>/ del host (el árbol btrfs remoto no tiene
        snapshots/): tar sobre ssh, con tiempo máximo. Mejor esfuerzo: OSError si falla."""
        import tempfile

        kit = Path(tempfile.mkdtemp(prefix="restore-fusion-kit-"))
        try:
            for rel, content in files.items():
                p = kit / rel
                p.write_bytes(content)
                if rel.endswith(".sh"):
                    p.chmod(0o755)
            remote = self.remote_for(dest)
            rc, err = self.ops.push_tree(kit, remote.ssh, remote.remote, remote.base, timeout=120)
            if rc != 0:
                raise OSError(f"recovery kit not written: {err or rc}")
        finally:
            shutil.rmtree(kit, ignore_errors=True)

    # -- retención: solo planes + ejecución con Approval ------------------------------------------------------------
    def plan_usb_retention(self, dest: LocalDestination, policy: RetentionPolicy) -> PrunePlan:
        return plan_prune(self.records(dest), policy, "btrfs")

    def prune_usb(
        self, dest: LocalDestination, names: List[str], approval: Optional[Approval]
    ) -> List[str]:
        """Borra snapshots del USB: primero los subvolúmenes recibidos (`btrfs subvolume delete`) y
        luego el directorio. Sin Approval que lo cubra no se toca nada."""

        def rm(snap: str) -> None:
            base = dest.snaps / snap
            for sub in sorted(base.iterdir()):
                if sub.is_dir() and not sub.is_symlink() and self.ops.is_subvolume(sub):
                    self.ops.delete_subvolume(sub)
            dest.remove(snap, approval)

        return apply_deletions(names, rm, approval).deleted

    def plan_local_prune(
        self, keep_names: Dict[str, str], protect: Sequence[str] = ()
    ) -> PrunePlan:
        """Snapshots locales RO de ESTA herramienta más allá de LOCAL_KEEP por kind. Solo nuestros
        kinds; jamás los de `keep_names`/`protect` (los padres registrados)."""
        keep_n = max(2, self.cfg.local_keep)
        keepers = set(keep_names.values()) | set(protect)
        plan = PrunePlan(engine="btrfs", keep_floor=keep_n)
        by_kind: Dict[str, List[str]] = {}
        d = self.local_dir
        if d.is_dir():
            for entry in d.iterdir():
                m = COMPACT_RE.match(entry.name)
                if entry.is_dir() and m and m.group(1) in self.kinds:
                    by_kind.setdefault(m.group(1), []).append(entry.name)
        for _kind, names in by_kind.items():
            names.sort()
            plan.over_cap += [n for n in names[:-keep_n] if n not in keepers]
        return plan

    def prune_local(self, plan: PrunePlan, approval: Optional[Approval]) -> List[str]:
        return apply_deletions(
            plan.over_cap, lambda n: self.ops.delete_subvolume(self.local_dir / n), approval
        ).deleted

    def plan_remote_retention(self, dest: SSHDestination) -> PrunePlan:
        return self.remote_for(dest).plan_retention()

    def prune_remote(self, dest: SSHDestination, plan: PrunePlan, approval, emit: Emit = no_emit):
        return self.remote_for(dest).prune(plan, approval, emit)


__all__ = ["BtrfsSendEngine", "kind_of", "item_id", "Baseline"]
