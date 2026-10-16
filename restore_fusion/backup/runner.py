"""Orquestador del backup: estado del sistema -> motor -> kit de recuperación -> retención.

Une las piezas de M1-M10 sin lógica propia de copia:
  * el motor (rsync o btrfs) copia y devuelve completed | partial | failed;
  * solo tras un `completed` se escribe el kit de recuperación y se corre la retención
    configurada (plan -> Approval -> borrado, nunca por debajo de MIN_KEEP);
  * en --dry-run no se escribe nada y se muestra lo que la retención borraría.
`--yes` no interviene aquí: la retención usa su propio Approval y las copias remotas a medias
exigen el Approval del usuario (heal_approval).
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..config.schema import Config
from ..destinations.base import RemoteQueryError
from ..destinations.local import LocalDestination
from ..destinations.ssh import SSHDestination
from ..engines.base import CopyEngine, EngineContext, EngineResult, no_emit
from ..model.manifest import COMPLETED, FAILED, PARTIAL, SnapshotRecord, pick_baseline
from ..packages.base import PackageManager
from ..safety.approval import Approval, apply_deletions
from ..safety.retention_plan import RetentionPolicy, plan_prune
from ..system_state.collector import SystemStateCollector
from . import kit as kitmod
from .retention import execute_retention

Emit = Callable[[str, str], None]


@dataclass
class RunSummary:
    results: List[EngineResult]
    pruned: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        sts = {r.status for r in self.results}
        if not sts:
            return FAILED
        if sts == {COMPLETED}:
            return COMPLETED
        if COMPLETED in sts or sts <= {COMPLETED, PARTIAL}:
            return PARTIAL
        return FAILED

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED

    @property
    def message(self) -> str:
        return "; ".join(f"{r.destination}: {r.status}" + (f" ({r.message})" if r.message else "")
                         for r in self.results if r.status != COMPLETED)  # fmt: skip


class BackupRunner:
    def __init__(
        self,
        cfg: Config,
        engine: CopyEngine,
        dests: List,
        pm: Optional[PackageManager],
        *,
        emit: Emit = no_emit,
        hostname: Optional[str] = None,
        tarball: Optional[bytes] = None,
        retention: bool = True,
        state_kw: Optional[dict] = None,
    ):
        self.cfg, self.engine, self.dests, self.pm = cfg, engine, list(dests), pm
        self.emit = emit
        self.hostname = hostname or os.uname().nodename
        self.tarball = tarball
        self.retention = retention
        self.state_kw = state_kw or {}

    # -- contexto -----------------------------------------------------------------
    def _ctx(self, stage: Optional[Path]) -> EngineContext:
        excl: List[str] = []
        if self.engine.id == "rsync":
            from ..engines.rsync_hardlink import exclusions_for

            excl = exclusions_for(self.cfg.exclude_defaults, self.cfg.extra_exclusions)
        return EngineContext(self.cfg.home, self.cfg.user, excl, stage, self.hostname)

    def _policy(self) -> RetentionPolicy:
        return RetentionPolicy(self.cfg.min_keep, self.cfg.max_snapshots)

    # -- corrida -------------------------------------------------------------------
    def run(self, dry_run: bool = False, heal_approval: Optional[Approval] = None) -> RunSummary:
        summary = RunSummary([])
        stage: Optional[Path] = None
        try:
            if not dry_run:
                stage = self._collect_state(summary)
            ctx = self._ctx(stage)
            summary.results = self._run_engine(ctx, dry_run, heal_approval)
            if dry_run:
                self._describe_retention()
                return summary
            for dest, res in zip(self.dests, summary.results):
                if res.status == COMPLETED:
                    self._write_kit(dest, res, summary)
                    summary.pruned[res.destination] = self._retain(dest, res, summary)
            self._prune_local_snapshots(summary)
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
        return summary

    def _collect_state(self, summary: RunSummary) -> Optional[Path]:
        if self.pm is None:
            msg = "unsupported distro: system state not captured (only your files are backed up)"
            summary.warnings.append(msg)
            self.emit("warning", msg)
            return None
        stage = Path(tempfile.mkdtemp(prefix="restore-fusion-stage-"))
        self.emit("stage", "System state (packages, config, restore.sh)")
        for w in SystemStateCollector(
            stage, self.pm, self.engine.id, **self.state_kw
        ).collect_all():
            summary.warnings.append(w)
            self.emit("warning", w)
        return stage

    def _run_engine(self, ctx, dry_run, heal_approval) -> List[EngineResult]:
        if hasattr(self.engine, "run_many"):  # btrfs: un juego de snapshots locales para todos
            return self.engine.run_many(self.dests, ctx, self.emit, dry_run, heal_approval)
        return [self.engine.run(d, ctx, self.emit, dry_run) for d in self.dests]

    # -- kit -------------------------------------------------------------------------
    def _write_kit(self, dest, res: EngineResult, summary: RunSummary) -> None:
        try:
            files = kitmod.kit_files(
                self.engine.id,
                dest,
                latest=res.snapshot_name,
                host=self.hostname,
                tarball=self.tarball,
            )
            if isinstance(dest, SSHDestination) and self.engine.id == "btrfs":
                self.engine.write_remote_kit(dest, files)
            else:
                dest.write_kit_files(files)
        except (OSError, ValueError) as exc:
            msg = f"{res.destination}: recovery kit not written: {exc}"
            summary.warnings.append(msg)
            self.emit("warning", msg)

    # -- retención ---------------------------------------------------------------------------
    def _records(self, dest) -> List[SnapshotRecord]:
        return [
            SnapshotRecord(n, dest.read_manifest(n), tuple(dest.list_entries(n)))
            for n in dest.list_snapshots()
        ]

    def _retain(self, dest, res: EngineResult, summary: RunSummary) -> List[str]:
        if not self.retention:
            return []
        try:
            if self.engine.id == "btrfs" and isinstance(dest, SSHDestination):
                plan = self.engine.plan_remote_retention(dest)
                for line in plan.describe():
                    self.emit("info", line)
                if not plan.over_cap:
                    return []
                from ..safety.retention_plan import approve_retention

                return self.engine.prune_remote(dest, plan, approve_retention(plan), self.emit)
            if self.engine.id == "btrfs":
                plan = self.engine.plan_usb_retention(dest, self._policy())
                remove = lambda names, ap: self.engine.prune_usb(dest, names, ap)  # noqa: E731
            else:
                plan = plan_prune(self._records(dest), self._policy(), self.engine.id)
                remove = lambda names, ap: apply_deletions(  # noqa: E731
                    names, lambda n: dest.remove(n, ap), ap
                ).deleted
            return execute_retention(dest, plan, remove, self.cfg.min_keep, self.emit)
        except (RemoteQueryError, OSError) as exc:
            msg = f"{res.destination}: retention skipped: {exc}"
            summary.warnings.append(msg)
            self.emit("warning", msg)
            return []

    def _describe_retention(self) -> None:
        """--dry-run: lo que la retención borraría (no borra nada)."""
        if not self.retention:
            return
        for dest in self.dests:
            try:
                if self.engine.id == "btrfs" and isinstance(dest, SSHDestination):
                    plan = self.engine.plan_remote_retention(dest)
                elif self.engine.id == "btrfs":
                    plan = self.engine.plan_usb_retention(dest, self._policy())
                else:
                    plan = plan_prune(self._records(dest), self._policy(), self.engine.id)
            except (RemoteQueryError, OSError) as exc:
                self.emit("warning", f"{dest.label}: cannot plan retention: {exc}")
                continue
            for line in plan.describe():
                self.emit("info", f"{dest.label}: {line}")

    def _prune_local_snapshots(self, summary: RunSummary) -> None:
        """btrfs: los snapshots locales RO de la herramienta más allá de LOCAL_KEEP (padres -p)."""
        if not (self.retention and self.engine.id == "btrfs"):
            return
        keep: Dict[str, str] = {}
        protect: List[str] = []
        for r in summary.results:
            keep.update(r.details.get("local_snapshots", {}) or {})
        for dest in self.dests:
            if isinstance(dest, LocalDestination):
                try:  # solo los padres del último completado: son la base del próximo envío
                    best = pick_baseline(self._records(dest), self.engine.id)
                except OSError:
                    best = None
                if best is not None:
                    protect += list((best.manifest.get("local_snapshots") or {}).values())
        plan = self.engine.plan_local_prune(keep, protect)
        if plan.over_cap:
            from ..safety.retention_plan import approve_retention

            done = self.engine.prune_local(plan, approve_retention(plan))
            summary.pruned["local"] = done
            for n in done:
                self.emit("info", f"pruned local snapshot: {n}")
