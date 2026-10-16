"""Manifest de snapshot (v1 + v2), máquina de estados y selección de baseline.

Regla 2 del README: una corrida parcial o fallida NUNCA es baseline ni mueve `latest`.

  * v1 = el manifest que escriben hoy las dos ediciones (sin campo `engine`).
  * v2 = igual + `engine` ("rsync" | "btrfs"). Los v1 se clasifican por sus campos o por el
    layout del directorio, sin reescribirlos (no hay migración: decisión 2).
  * El baseline solo sale de snapshots `completed` del MISMO motor. Si el destino solo tiene
    historia del otro motor (o de un motor que no se puede identificar), la corrida cuenta
    como completa y pasa por la confirmación de backup completo.

Estados: running -> completed | partial | failed. Todos los finales son terminales: un
snapshot partial/failed no se "promueve" a completed. Un `running` que quedó tras un kill
se trata como no-completado.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

MANIFEST_VERSION = 2

RUNNING = "running"
COMPLETED = "completed"
PARTIAL = "partial"
FAILED = "failed"
UNKNOWN = "unknown"

FINAL_STATUSES = frozenset({COMPLETED, PARTIAL, FAILED})
VALID_STATUSES = frozenset({RUNNING, COMPLETED, PARTIAL, FAILED})
ENGINES = ("rsync", "btrfs")

SNAPSHOT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d)?$")

# Campos que solo escribe una edición (para clasificar manifests v1)
_RSYNC_KEYS = ("link_dest_baseline", "rsync_exit", "source_home")
_BTRFS_KEYS = ("local_snapshots", "parents", "target_is_btrfs")


def is_valid_snapshot_name(name: str) -> bool:
    return bool(SNAPSHOT_NAME_RE.match(name or ""))


def parse_manifest(text: str) -> dict:
    """JSON -> dict; texto ilegible o que no sea un objeto -> {} (nunca lanza)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status_of(manifest: dict) -> str:
    s = manifest.get("status") if isinstance(manifest, dict) else None
    return s if s in VALID_STATUSES else UNKNOWN


def classify_engine(manifest: dict, entries: Iterable[str] = ()) -> Optional[str]:
    """Motor que produjo el snapshot, o None si no se puede saber (nunca se adivina).

    1. campo `engine` (v2);
    2. campos propios de una edición (v1);
    3. layout del directorio: `home` -> rsync; `root_*`/`home_*`/`*.btrfs.zst` -> btrfs.
    Pistas contradictorias -> None.
    """
    declared = manifest.get("engine") if isinstance(manifest, dict) else None
    if declared in ENGINES:
        return declared
    if declared not in (None, ""):
        return None  # motor desconocido: no es de los nuestros

    found = set()
    if any(k in manifest for k in _RSYNC_KEYS):
        found.add("rsync")
    if any(k in manifest for k in _BTRFS_KEYS):
        found.add("btrfs")
    names = list(entries)
    if "home" in names:
        found.add("rsync")
    if any(n.startswith(("root_", "home_")) or n.endswith(".btrfs.zst") for n in names):
        found.add("btrfs")
    return found.pop() if len(found) == 1 else None


def new_manifest(engine: str, name: str, status: str, **fields) -> dict:
    """Manifest v2. `fields` pisa los básicos."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    data = {
        "manifest_version": MANIFEST_VERSION,
        "engine": engine,
        "snapshot": name,
        "status": status,
    }
    data.update(fields)
    return data


def can_transition(old: Optional[str], new: str) -> bool:
    if new not in VALID_STATUSES:
        return False
    if old is None:
        return new == RUNNING
    if old == RUNNING:
        return new in FINAL_STATUSES
    return False  # los estados finales son terminales


def transition(manifest: dict, new: str, **fields) -> dict:
    """Copia del manifest con el nuevo estado; ValueError si la transición no es válida."""
    old = manifest.get("status") if manifest else None
    if not can_transition(old, new):
        raise ValueError(f"invalid status transition: {old!r} -> {new!r}")
    out = dict(manifest)
    out.update(fields)
    out["status"] = new
    return out


def may_update_latest(status: str) -> bool:
    """`latest` solo se mueve a un snapshot `completed`."""
    return status == COMPLETED


@dataclass(frozen=True)
class SnapshotRecord:
    name: str
    manifest: dict = field(default_factory=dict)
    entries: tuple = ()  # nombres dentro del directorio del snapshot (para clasificar v1)

    @property
    def status(self) -> str:
        return status_of(self.manifest)

    @property
    def engine(self) -> Optional[str]:
        return classify_engine(self.manifest, self.entries)


def pick_baseline(records: Iterable[SnapshotRecord], engine: str) -> Optional[SnapshotRecord]:
    """El snapshot completed más nuevo DEL MISMO MOTOR; None => la corrida es completa.

    Nunca elige partial/failed/running/unknown, ni uno de otro motor, ni uno cuyo motor no
    se pueda identificar, ni un nombre que no sea de snapshot.
    """
    best: Optional[SnapshotRecord] = None
    for r in records:
        if not is_valid_snapshot_name(r.name):
            continue
        if r.status != COMPLETED or r.engine != engine:
            continue
        if best is None or r.name > best.name:
            best = r
    return best


def would_be_full(records: Iterable[SnapshotRecord], engine: str) -> bool:
    return pick_baseline(records, engine) is None


def history_of_other_engine(records: Iterable[SnapshotRecord], engine: str) -> List[str]:
    """Nombres de snapshots completed de otro motor (para explicar por qué la corrida es completa)."""
    return sorted(
        r.name
        for r in records
        if is_valid_snapshot_name(r.name)
        and r.status == COMPLETED
        and r.engine in ENGINES
        and r.engine != engine
    )


def index_by_name(records: Iterable[SnapshotRecord]) -> Dict[str, SnapshotRecord]:
    return {r.name: r for r in records}
