"""Plan de retención: calcula QUÉ se borraría; no borra nada (decisión 1).

Orden de las reglas (igual en las dos ediciones):
  1. stale: snapshots partial/failed/running MÁS VIEJOS que el completed más nuevo.
  2. over_cap: si MAX_SNAPSHOTS > 0, los completed más viejos que sobran.
  3. budget: mientras el destino esté sobre presupuesto (disco %, GB remotos), el completed
     más viejo. Cuántos hace falta depende del uso real del disco, que cambia con cada
     borrado; por eso el plan da el ORDEN y el TOPE (`budget_limit`) y el ejecutor mide.

Piso: nunca por debajo de MIN_KEEP (mínimo 1) y nunca el último completed, ni siquiera por
MAX_SNAPSHOTS. Solo se consideran snapshots del motor que se está podando: la historia del
otro motor, o de uno no identificable, no aparece jamás en un plan.
"""

from dataclasses import dataclass, field
from typing import Iterable, List

from ..model.manifest import (
    COMPLETED,
    FAILED,
    PARTIAL,
    RUNNING,
    SnapshotRecord,
    is_valid_snapshot_name,
)
from .approval import ORIGIN_RETENTION, _MINT_KEY, Approval

_STALE = (PARTIAL, FAILED, RUNNING)


@dataclass(frozen=True)
class RetentionPolicy:
    min_keep: int = 2
    max_snapshots: int = 0  # 0 = sin tope duro

    @property
    def floor(self) -> int:
        return max(1, int(self.min_keep))


@dataclass
class PrunePlan:
    engine: str
    keep_floor: int
    stale: List[str] = field(default_factory=list)
    over_cap: List[str] = field(default_factory=list)
    budget_order: List[str] = field(default_factory=list)  # más viejo primero
    budget_limit: int = 0  # cuántos de budget_order como máximo

    @property
    def unconditional(self) -> List[str]:
        return [*self.stale, *self.over_cap]

    def budget_candidates(self) -> List[str]:
        """Los únicos que el ejecutor puede borrar por presupuesto, en orden."""
        return self.budget_order[: self.budget_limit]

    @property
    def all_names(self) -> List[str]:
        return [*self.unconditional, *self.budget_candidates()]

    def describe(self) -> List[str]:
        """Líneas para --dry-run y para el prompt."""
        lines = [
            f"[{self.engine}] retention keeps at least {self.keep_floor} completed snapshot(s)"
        ]
        lines += [f"would prune (stale, not usable as baseline): {n}" for n in self.stale]
        lines += [f"would prune (over MAX_SNAPSHOTS): {n}" for n in self.over_cap]
        if self.budget_candidates():
            lines.append(
                f"if the target is over budget, up to {self.budget_limit} more, oldest first:"
            )
            lines += [f"  {n}" for n in self.budget_candidates()]
        if len(lines) == 1:
            lines.append("nothing to prune")
        return lines


def plan_prune(
    records: Iterable[SnapshotRecord], policy: RetentionPolicy, engine: str
) -> PrunePlan:
    own = [r for r in records if is_valid_snapshot_name(r.name) and r.engine == engine]
    completed = sorted((r.name for r in own if r.status == COMPLETED))
    newest = completed[-1] if completed else ""
    floor = policy.floor
    plan = PrunePlan(engine=engine, keep_floor=floor)

    if newest:
        plan.stale = sorted(r.name for r in own if r.status in _STALE and r.name < newest)

    remaining = list(completed)
    if policy.max_snapshots > 0:
        keep = max(policy.max_snapshots, floor)
        if len(remaining) > keep:
            plan.over_cap = remaining[: len(remaining) - keep]
            remaining = remaining[len(remaining) - keep :]

    plan.budget_order = list(remaining)
    plan.budget_limit = max(0, len(remaining) - floor)
    return plan


def approve_retention(plan: PrunePlan, *, retention_enabled: bool = True) -> Approval:
    """Aprobación de la retención que el usuario CONFIGURÓ, limitada a los nombres del plan.

    No recibe `--yes`. Si la retención está desactivada no se crea ninguna aprobación.
    """
    if not retention_enabled:
        raise PermissionError("retention is disabled")
    return Approval._mint(_MINT_KEY, "delete", plan.all_names, ORIGIN_RETENTION)
