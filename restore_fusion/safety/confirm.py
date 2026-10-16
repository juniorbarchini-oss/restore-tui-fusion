"""Confirmaciones: por defecto es No; sin terminal es No; EOF/Ctrl-C es No.

`--yes` puede saltarse la confirmación de un backup COMPLETO (`confirm_action`) pero no puede
aprobar un borrado: `ask_delete` no tiene ningún parámetro "yes".
"""

import sys
from typing import Callable, Optional, Sequence

from .approval import ORIGIN_PROMPT, _MINT_KEY, Approval

_YES = ("y", "yes")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(
    question: str,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
    is_tty: Optional[Callable[[], bool]] = None,
) -> bool:
    """[y/N]. Solo 'y'/'yes' es Sí. Sin terminal no se pregunta: es No."""
    # se resuelven al LLAMAR (no al definir): así se pueden sustituir en pruebas y en el CLI
    is_tty = is_tty or _stdin_is_tty
    input_fn = input_fn or input
    if not is_tty():
        return False
    try:
        return input_fn(f"{question} [y/N] ").strip().lower() in _YES
    except (EOFError, KeyboardInterrupt):
        return False


def confirm_action(
    question: str,
    *,
    assume_yes: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
    is_tty: Optional[Callable[[], bool]] = None,
) -> bool:
    """Para acciones NO destructivas (p. ej. un backup completo): `--yes` las aprueba."""
    if assume_yes:
        return True
    return confirm(question, input_fn=input_fn, is_tty=is_tty)


def ask_delete(
    question: str,
    items: Sequence[str],
    *,
    action: str = "delete",
    input_fn: Optional[Callable[[str], str]] = None,
    is_tty: Optional[Callable[[], bool]] = None,
    print_fn: Callable[[str], None] = print,
) -> Optional[Approval]:
    """Lista lo que se borraría y pregunta. Devuelve un Approval solo si el usuario escribió y/yes.

    A propósito NO acepta `assume_yes`: un flag --yes nunca llega hasta aquí.
    """
    items = list(items)
    is_tty = is_tty or _stdin_is_tty
    if not items or not is_tty():
        return None
    for n in items:
        print_fn(f"  {n}")
    if not confirm(question, input_fn=input_fn, is_tty=is_tty):
        return None
    return Approval._mint(_MINT_KEY, action, items, ORIGIN_PROMPT)
