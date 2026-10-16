"""Ejecuta la retención CONFIGURADA (decisión 1): plan_prune -> Approval -> borrado.

  * lo incondicional (parciales viejos, sobrante de MAX_SNAPSHOTS) se borra siempre que el plan lo
    liste;
  * el presupuesto (disco % / GB remotos) se borra de a uno, el más viejo primero, SOLO mientras el
    destino siga sobre presupuesto, y nunca por debajo de MIN_KEEP ni el último completado.
Nada se borra sin un Approval que lo cubra (el flag --yes no interviene).
"""

from typing import Callable, List

from ..safety.approval import Approval
from ..safety.retention_plan import PrunePlan, approve_retention

Remove = Callable[[List[str], Approval], List[str]]
Emit = Callable[[str, str], None]


def execute_retention(
    dest, plan: PrunePlan, remove: Remove, min_keep: int, emit: Emit
) -> List[str]:
    for line in plan.describe():
        emit("info", line)
    if not plan.all_names:
        return []
    approval = approve_retention(plan)
    pruned: List[str] = []
    if plan.unconditional:
        pruned += remove(plan.unconditional, approval)
    remaining = len(plan.budget_order)
    emit("info", dest.usage_summary())
    for name in plan.budget_candidates():
        if not dest.should_prune_more(remaining, min_keep):
            break
        pruned += remove([name], approval)
        remaining -= 1
    for name in pruned:
        emit("info", f"pruned snapshot: {name}")
    return pruned
