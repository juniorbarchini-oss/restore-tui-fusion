"""Approval: la única forma de borrar algo.

Regla 4 del README: nunca se borra solo; `--yes` NO aprueba borrados; el prompt por defecto
es No; sin terminal es No.

Un `Approval` solo lo crean:
  * `confirm.ask_delete()`  -> el usuario contestó "y" a un prompt que lista lo que se borra;
  * `retention_plan.approve_retention()` -> la política de retención que el usuario configuró
    (decisión 1), limitada a los nombres que el plan lista.
Ninguna de las dos funciones recibe el flag `--yes`. El constructor está protegido: llamarlo
directamente falla. `apply_deletions()` rechaza (antes de borrar nada) cualquier nombre que
el Approval no cubra.
"""

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional

ORIGIN_PROMPT = "user-prompt"
ORIGIN_RETENTION = "retention-config"

_MINT_KEY = object()  # solo los módulos de este paquete lo importan


class ApprovalError(PermissionError):
    """Se intentó borrar sin una aprobación que lo cubra."""


class Approval:
    __slots__ = ("_action", "_items", "_origin")

    def __init__(self, *args, **kwargs):
        raise TypeError("Approval cannot be built directly; ask the user (confirm.ask_delete)")

    @classmethod
    def _mint(cls, key, action: str, items: Iterable[str], origin: str) -> "Approval":
        if key is not _MINT_KEY:
            raise TypeError("Approval cannot be built directly")
        if origin not in (ORIGIN_PROMPT, ORIGIN_RETENTION):
            raise ValueError(f"unknown approval origin: {origin!r}")
        self = object.__new__(cls)
        object.__setattr__(self, "_action", action)
        object.__setattr__(self, "_items", frozenset(items))
        object.__setattr__(self, "_origin", origin)
        return self

    def __setattr__(self, name, value):
        raise AttributeError("Approval is immutable")

    @property
    def action(self) -> str:
        return self._action

    @property
    def items(self) -> FrozenSet[str]:
        return self._items

    @property
    def origin(self) -> str:
        return self._origin

    def covers(self, action: str, name: str) -> bool:
        return action == self._action and name in self._items

    def __repr__(self) -> str:
        return f"Approval({self._action!r}, {len(self._items)} item(s), {self._origin})"


@dataclass
class DeleteReport:
    deleted: List[str]
    failed: Dict[str, str]


def apply_deletions(
    names: Iterable[str],
    delete_fn: Callable[[str], None],
    approval: Optional[Approval],
    *,
    action: str = "delete",
) -> DeleteReport:
    """Borra solo lo que `approval` cubre. Si algo no está cubierto no se borra NADA."""
    names = list(names)
    if approval is None:
        raise ApprovalError("deletion refused: no approval")
    uncovered = [n for n in names if not approval.covers(action, n)]
    if uncovered:
        raise ApprovalError(f"deletion refused: not approved: {', '.join(sorted(uncovered))}")
    report = DeleteReport([], {})
    for n in names:
        try:
            delete_fn(n)
            report.deleted.append(n)
        except (OSError, ValueError, RuntimeError) as exc:
            report.failed[n] = str(exc)
    return report
