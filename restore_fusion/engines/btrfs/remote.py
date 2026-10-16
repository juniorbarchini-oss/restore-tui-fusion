"""Árbol btrfs en un host SSH:  <base>/{root,home}/<nombre>  (subvolúmenes recibidos)  y
<base>/meta/<nombre>/ (restore.sh + _system_state).  NO hay manifest.json en este layout.

La integridad de una copia remota se decide por el mecanismo de btrfs, no por un manifest
(hallazgo en el backup real, ver PR de M9):
  * completa  = subvolumen de SOLO LECTURA con `received_uuid` (un `btrfs receive` terminado);
  * a medias  = lectura-escritura y received_uuid "-" (un receive interrumpido).
Solo una copia completa que además existe localmente en /.snapshots sirve de padre `send -p`.

De btrfs-restore-tui/backup.py (_remote_parent, remote_partial_copies, _heal_remote,
_prune_remote, _push_state_remote). Cambios:
  * borrar (copias a medias o retención) exige un `Approval` que cubra `kind/nombre`;
  * el comando remoto se cita con shlex.join (en pgrep el patrón va SIN comillas literales,
    porque ya no pasa por el shell remoto con el split de espacios del original);
  * la poda de retención itera los `kinds` configurados, no ("root", "home") fijos.
"""

import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ...destinations.base import RemoteQueryError
from ...safety.approval import Approval, apply_deletions
from ...safety.retention_plan import PrunePlan
from ..baseline import ORIGIN_RECEIVED_UUID, Baseline, BaselineSource

# "<kind>_YYYYMMDD_HHMMSS[_N]"
COMPACT_RE = re.compile(r"^([A-Za-z0-9-]+)_\d{8}_\d{6}(?:_\d)?$")
Emit = Callable[[str, str], None]


def has_received_uuid(line: str) -> bool:
    parts = line.split()
    try:
        return parts[parts.index("received_uuid") + 1] != "-"
    except (ValueError, IndexError):
        return False


def parse_subvol(line: str) -> dict:
    """Línea de `btrfs subvolume list -u -q -R` -> {uuid, parent_uuid, received_uuid, path}."""
    out = {}
    parts = line.split()
    for key in ("uuid", "parent_uuid", "received_uuid"):
        if key in parts and parts.index(key) + 1 < len(parts):
            out[key] = parts[parts.index(key) + 1]
    if "path" in line:
        out["path"] = line.split("path", 1)[1].strip()
    return out


def item_id(kind: str, name: str) -> str:
    return f"{kind}/{name}"


class RemoteBtrfs:
    def __init__(
        self,
        ops,
        ssh_base: Sequence[str],
        remote: str,
        base: str,
        kinds: Sequence[str],
        local_snapshots_dir: Path,
        keep: int = 10,
    ):
        self.ops = ops
        self.ssh = list(ssh_base)
        self.remote = remote
        self.base = base.rstrip("/")
        self.kinds = list(kinds)
        self.local_dir = Path(local_snapshots_dir)
        self.keep = max(2, keep)

    # -- consultas ------------------------------------------------------------------
    def _ask(self, argv: List[str], what: str):
        try:
            return self.ops.ssh_capture(self.ssh, self.remote, argv)
        except subprocess.TimeoutExpired as exc:
            raise RemoteQueryError(f"{what}: timed out after {exc.timeout}s") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteQueryError(f"{what}: {exc}") from exc

    def _list(self, kind: str, flags: List[str], what: str):
        res = self._ask(["sudo", "btrfs", "subvolume", "list", *flags, f"{self.base}/{kind}"], what)
        if res.returncode != 0:
            raise RemoteQueryError(
                f"{what} (exit {res.returncode}): {(res.stderr or '').strip() or 'no error text'}"
            )
        return res

    def baseline(self, kind: str) -> Optional[Baseline]:
        """Copia COMPLETA más nueva (solo lectura + received_uuid) que además existe en local.

        None => el remoto respondió y no tiene base usable (un envío completo es legítimo).
        Si no se pudo preguntar, RemoteQueryError: nunca se finge que está vacío."""
        what = f"could not list {kind} copies on the remote"
        exists = self._ask(["test", "-d", f"{self.base}/{kind}"], what)
        if exists.returncode == 1:
            return None  # el remoto respondió: la carpeta todavía no existe
        if exists.returncode != 0:
            raise RemoteQueryError(
                f"{what} (exit {exists.returncode}): "
                f"{(exists.stderr or '').strip() or 'no error text'}"
            )
        res = self._list(kind, ["-o", "-r", "-R"], what)
        # -r lista solo lectura y -R añade received_uuid: una copia a medias es lectura-escritura
        # con received_uuid "-", así que jamás se elige como base.
        names = sorted(
            line.split("path", 1)[1].strip().split("/")[-1]
            for line in res.stdout.splitlines()
            if "path" in line and has_received_uuid(line)
        )
        names = [n for n in names if COMPACT_RE.match(n) and n.startswith(f"{kind}_")]
        for n in reversed(names):
            local = self.local_dir / n
            if local.is_dir():
                return Baseline(n, ORIGIN_RECEIVED_UUID, kind, local, None)
        return None

    def full_kinds(self) -> List[str]:
        return [k for k in self.kinds if self.baseline(k) is None]

    def partial_copies(self) -> List[dict]:
        """Copias a medias bajo <base>/<kind>: {kind, name, has_children}. No borra nada."""
        found, all_subvols = [], []
        for kind in self.kinds:
            what = f"could not list {kind} copies on the remote"
            exists = self._ask(["test", "-d", f"{self.base}/{kind}"], what)
            if exists.returncode == 1:
                continue
            allq = self._list(kind, ["-o", "-u", "-q", "-R"], what)
            roq = self._list(kind, ["-o", "-r"], what)
            ro_paths = {
                parse_subvol(ln).get("path") for ln in roq.stdout.splitlines() if "path" in ln
            }
            subs = [parse_subvol(ln) for ln in allq.stdout.splitlines() if "path" in ln]
            all_subvols += subs
            for sv in subs:
                name = sv["path"].split("/")[-1]
                if (
                    COMPACT_RE.match(name)
                    and name.startswith(f"{kind}_")
                    and sv["path"] not in ro_paths
                    and sv.get("received_uuid") == "-"
                    and sv.get("uuid")
                ):
                    found.append({"kind": kind, "name": name, "uuid": sv["uuid"]})
        for item in found:
            item["has_children"] = any(sv.get("parent_uuid") == item["uuid"] for sv in all_subvols)
            del item["uuid"]
        return found

    def statuses(self, kind: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        partial = {p["name"] for p in self.partial_copies() if p["kind"] == kind}
        res = self._list(kind, ["-o", "-r", "-R"], f"could not list {kind} copies on the remote")
        for line in res.stdout.splitlines():
            if "path" in line and has_received_uuid(line):
                n = line.split("path", 1)[1].strip().split("/")[-1]
                if COMPACT_RE.match(n) and n.startswith(f"{kind}_"):
                    out[n] = "completed"
        for n in partial:
            out[n] = "partial"
        return out

    # -- borrar: solo con Approval -------------------------------------------------------
    def _delete_subvol(self, ident: str) -> None:
        kind, name = ident.split("/", 1)
        d = self.ops.ssh_capture(
            self.ssh,
            self.remote,
            ["sudo", "btrfs", "subvolume", "delete", f"{self.base}/{kind}/{name}"],
        )
        if d.returncode != 0:
            raise OSError((d.stderr or "").strip() or f"exit {d.returncode}")

    def heal(
        self,
        approval: Optional[Approval],
        sending: Dict[str, str],
        emit: Emit,
    ) -> List[str]:
        """Borra las copias a medias que el usuario aprobó, re-comprobando cada salvaguarda justo
        antes: nombre con marca de tiempo, lectura-escritura sin received_uuid, nada que dependa
        de ella, ningún `btrfs receive` corriendo, y no la copia que se está enviando. Cualquier
        consulta fallida => no se borra nada. Devuelve los ids borrados."""
        if approval is None or not approval.items:
            return []
        try:
            alive = self._ask(
                ["pgrep", "-f", "[b]trfs receive"], "could not check for a running btrfs receive"
            )
            if alive.returncode != 1:
                emit(
                    "warning",
                    f"{self.remote}: a btrfs receive is running (or could not be checked) - "
                    "not deleting partial copies",
                )
                return []
            current = {(c["kind"], c["name"]): c for c in self.partial_copies()}
        except RemoteQueryError as exc:
            emit("warning", f"{self.remote}: partial copies not deleted - {exc}")
            return []
        deletable: List[str] = []
        for ident in sorted(approval.items):
            kind, _, name = ident.partition("/")
            item = current.get((kind, name))
            if item is None:
                emit("info", f"{self.remote}: {name} is no longer a partial copy - kept")
            elif item["has_children"]:
                emit("warning", f"{self.remote}: {name} has snapshots depending on it - kept")
            elif name == sending.get(kind):
                emit("warning", f"{self.remote}: {name} is the copy being sent - kept")
            else:
                deletable.append(ident)
        rep = apply_deletions(deletable, self._delete_subvol, approval)
        for ident in rep.deleted:
            kind, _, name = ident.partition("/")
            emit(
                "warning",
                f"{self.remote}: deleted partial copy {kind}/{name} "
                "(read-write, no received_uuid: an interrupted receive)",
            )
        for ident, why in rep.failed.items():
            emit("warning", f"{self.remote}: could not delete {ident}: {why}")
        return rep.deleted

    def plan_retention(self) -> PrunePlan:
        """Qué copias remotas sobran (las más viejas más allá de `keep` por kind). Solo nombres con
        marca de tiempo: un nombre hecho a mano no cuenta como "el más nuevo" ni se borra.
        No borra nada: el ejecutor necesita un Approval (`approve_retention(plan)`)."""
        plan = PrunePlan(engine="btrfs", keep_floor=self.keep)
        for kind in self.kinds:
            res = self._list(kind, ["-o"], f"could not list {kind} copies on the remote")
            names = sorted(
                ln.split("path", 1)[1].strip().split("/")[-1]
                for ln in res.stdout.splitlines()
                if "path" in ln
            )
            names = [n for n in names if COMPACT_RE.match(n) and n.startswith(f"{kind}_")]
            plan.over_cap += (
                [item_id(kind, n) for n in names[: -self.keep]] if len(names) > self.keep else []
            )
        return plan

    def prune(self, plan: PrunePlan, approval: Optional[Approval], emit: Emit) -> List[str]:
        rep = apply_deletions(plan.over_cap, self._delete_subvol, approval)
        for ident in rep.deleted:
            emit("info", f"{self.remote}: pruned {ident.split('/', 1)[1]}")
        for ident, why in rep.failed.items():
            emit("warning", f"{self.remote}: prune {ident} failed: {why}")
        return rep.deleted

    # -- envío --------------------------------------------------------------------------------------
    def ensure_dirs(self) -> Tuple[bool, str]:
        mk = self._ask(
            ["mkdir", "-p", *[f"{self.base}/{k}" for k in self.kinds], f"{self.base}/meta"],
            "could not create the remote folders",
        )
        return mk.returncode == 0, (mk.stderr or "").strip() or "ssh failed"

    def send(self, kind: str, local: Path, parent: Optional[Path]) -> int:
        return self.ops.send_ssh_receive(
            local,
            parent,
            self.ssh,
            self.remote,
            ["sudo", "btrfs", "receive", f"{self.base}/{kind}/"],
        )

    def push_state(self, name: str, state_dir: Path) -> Tuple[int, str]:
        return self.ops.push_tree(
            state_dir, self.ssh, self.remote, f"{self.base}/meta/{name}", timeout=120
        )


class ReceivedUuidBaselineSource(BaselineSource):
    """BaselineSource sobre el árbol remoto de btrfs (sin manifests)."""

    origin = ORIGIN_RECEIVED_UUID

    def __init__(self, remote: RemoteBtrfs):
        self.remote = remote

    def baseline(self, kind: Optional[str] = None) -> Optional[Baseline]:
        if kind is None:
            raise ValueError("the remote btrfs tree has one baseline per kind (root/home)")
        return self.remote.baseline(kind)

    def statuses(self, kind: Optional[str] = None) -> Dict[str, str]:
        if kind is None:
            out: Dict[str, str] = {}
            for k in self.remote.kinds:
                out.update(self.remote.statuses(k))
            return out
        return self.remote.statuses(kind)
