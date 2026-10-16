"""Destinos candidatos para un backup: la unidad USB (si hay una válida) y/o el host SSH.

Regla 1 del README: la unidad sale de `resolve_target`, que exige un punto de montaje real y no
cae jamás a "la primera unidad que aparezca". Si no hay unidad válida el motivo se devuelve (no se
oculta) y el backup sigue solo si hay remoto.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import resolve_target
from ..config.schema import Config
from ..destinations.local import LocalDestination
from ..destinations.ssh import SSHDestination


@dataclass
class Candidates:
    dests: List = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # por qué NO hay USB / remoto


def candidate_destinations(
    cfg: Config, engine_id: str, want: Sequence[str] = ("usb", "remote"), **resolve_kw
) -> Candidates:
    out = Candidates()
    if "usb" in want:
        t = resolve_target(cfg, engine_id, **resolve_kw)
        if t.ok and t.root is not None:
            out.dests.append(
                LocalDestination(t.root, max_disk_percent=cfg.max_disk_percent, label="USB")
            )
        else:
            out.notes.append(f"USB: {t.reason}")
    if "remote" in want:
        if cfg.remote_configured:
            out.dests.append(
                SSHDestination(
                    host=cfg.remote_host,
                    host_lan=cfg.remote_host_lan,
                    root=cfg.remote_root(engine_id),
                    user=cfg.remote_user or cfg.user,
                    port=cfg.remote_port,
                    label=cfg.remote_name or cfg.remote_host,
                    remote_max_gb=cfg.remote_max_gb,
                    home=Path(cfg.home),
                )
            )
        else:
            out.notes.append("remote: not configured (set REMOTE_HOST and REMOTE_PATH)")
    return out
