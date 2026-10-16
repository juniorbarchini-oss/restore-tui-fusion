"""De dónde se puede restaurar: árbol de snapshots (USB / montaje sshfs), snapshots locales
(`/.snapshots` + Snapper) y el host remoto btrfs.

Fusión de btrfs-restore-tui/scanner.py y restore-tui/local_client.py (list_snapshots). Cambios:
  * un solo modelo (`model/snapshots.py`) con orígenes y áreas; un backup presente en USB y remoto
    es una entrada;
  * el remoto btrfs se lista con `received_uuid` (mismo mecanismo que el backup, M9): una copia
    a medias NO se ofrece para restaurar;
  * el ssh usa las opciones compartidas (`StrictHostKeyChecking=accept-new`; el original usaba `no`).
Nada aquí escribe ni borra.
"""

import json
import logging
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..destinations.ssh import build_ssh_argv, user_ssh_files
from ..engines.btrfs.remote import COMPACT_RE, has_received_uuid
from ..model.manifest import (
    RUNNING,
    FAILED,
    classify_engine,
    is_valid_snapshot_name,
    parse_manifest,
)
from ..model.snapshots import (
    Area, Origin, Snapshot, Storage, key_from_compact, parse_key_time,
)  # fmt: skip

logger = logging.getLogger("restore_fusion")

_USABLE = ("completed", "partial")


def _read_manifest(snap: Path) -> dict:
    try:
        return parse_manifest((snap / "manifest.json").read_text())
    except OSError:
        return {}


def _entry_time(snap: Path, manifest: dict, key: str) -> datetime:
    created = manifest.get("created_at") or manifest.get("started_at")
    if created:
        try:
            return datetime.fromisoformat(created)
        except ValueError:
            pass
    return parse_key_time(key) or datetime.fromtimestamp(snap.stat().st_mtime)


def home_area_for(base: Path, user: str, home: Path, label: str = "Home") -> Area:
    """Área Home de un subvolumen `home_<ts>`: si contiene la carpeta del usuario se entra a ella y
    el destino original es su $HOME; si no, el área es /home completo."""
    user_dir = base / user
    if user_dir.is_dir():
        return Area("home", label, user_dir, home, home_user_subdir=True)
    return Area("home", label, base, Path("/home"))


def scan_tree(
    snaps_dir: Path, *, label: str, kind: str, user: str, home: Path, mount: Optional[Path] = None
) -> List[Snapshot]:
    """Snapshots de `<root>/snapshots/` (USB, o ese árbol montado por sshfs), de cualquier motor.

    El estado sale del manifest: running/failed no se ofrecen; partial se ofrece marcado; sin manifest
    (backup viejo) es 'unknown'. La clasificación del motor es la de model/manifest.py.
    """
    out: List[Snapshot] = []
    try:
        entries = sorted(snaps_dir.iterdir(), key=lambda p: p.name, reverse=True)
    except OSError as exc:
        logger.warning("cannot list %s: %s", snaps_dir, exc)
        return out
    for snap in entries:
        if snap.is_symlink() or not snap.is_dir() or not is_valid_snapshot_name(snap.name):
            continue
        manifest = _read_manifest(snap)
        status = manifest.get("status", "unknown")
        if status in (RUNNING, FAILED):
            continue
        try:
            names = [p.name for p in snap.iterdir()]
        except OSError:
            continue
        engine = classify_engine(manifest, names)
        areas: List[Area] = []
        if (snap / "home").is_dir() and engine != "btrfs":
            areas.append(Area("home", "Home", snap / "home", home))
        else:
            for k in ("home", "root"):
                sub = next(iter(sorted(snap.glob(f"{k}_*"))), None)
                stream = snap / f"{k}.btrfs.zst"
                if sub is not None and sub.is_dir():
                    areas.append(
                        home_area_for(sub, user, home)
                        if k == "home"
                        else Area("root", "Root /", sub, Path("/"))
                    )
                elif stream.is_file():
                    if k == "home":
                        areas.append(Area("home", "Home", stream, home, Storage.STREAM, True))
                    else:
                        areas.append(Area("root", "Root /", stream, Path("/"), Storage.STREAM))
        if not areas:
            continue
        out.append(
            Snapshot(
                snap.name,
                _entry_time(snap, manifest, snap.name),
                status if status in _USABLE else "unknown",
                [Origin(label, kind, areas, engine, mount)],
            )
        )
    return out


def scan_local(snaps_dir: Path, *, user: str, home: Path) -> List[Snapshot]:
    """`/.snapshots`: snapshots RO propios (`home_<ts>`, `root_<ts>`), los maestros `*_parent` y los de
    Snapper. Cada uno es una entrada propia (el origen 'Local' es instantáneo: sin red ni staging).
    """
    out: List[Snapshot] = []
    if not snaps_dir.is_dir():
        return out
    try:
        entries = sorted(snaps_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for e in entries:
        try:
            if e.name in ("home_parent", "root_parent"):
                st = e.stat()
                when = max(datetime.fromtimestamp(st.st_mtime), datetime.fromtimestamp(st.st_ctime))
                if e.name == "home_parent":
                    area = home_area_for(e, user, home)
                    title = "home_parent [User /home]"
                else:
                    area = Area("root", "Root /", e, Path("/"))
                    title = "root_parent [System /]"
                out.append(Snapshot(f"local:{e.name}", when, "completed",
                                    [Origin("Local", "local", [area], "btrfs")], title))  # fmt: skip
                continue
            m = re.fullmatch(r"(home|root)_(\d{8}_\d{6})(?:_\d)?", e.name)
            if m and e.is_dir():
                when = datetime.strptime(m[2], "%Y%m%d_%H%M%S")
                area = (
                    home_area_for(e, user, home)
                    if m[1] == "home"
                    else Area("root", "Root /", e, Path("/"))
                )
                key = key_from_compact(m[2]) or f"local:{e.name}"
                out.append(
                    Snapshot(key, when, "completed", [Origin("Local", "local", [area], "btrfs")])
                )
                continue
            if (
                e.is_dir()
                and e.name.isdigit()
                and (e / "info.xml").exists()
                and (e / "snapshot").exists()
            ):
                root = ET.parse(e / "info.xml").getroot()
                num = root.findtext("num", e.name)
                desc = root.findtext("description", "")
                try:
                    when = datetime.strptime(root.findtext("date", ""), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    when = datetime.fromtimestamp(e.stat().st_mtime)
                out.append(Snapshot(
                    f"local:snapper_{num}", when, "completed",
                    [Origin("Local", "local", [Area("root", "Root /", e / "snapshot", Path("/"))], "btrfs")],
                    f"Snapper #{num} [{desc or 'System'}]",
                ))  # fmt: skip
        except (OSError, ET.ParseError, ValueError) as exc:
            logger.warning("skipping local snapshot %s: %s", e.name, exc)
    return sorted(out, key=lambda s: s.timestamp, reverse=True)


# ---- host remoto btrfs: <base>/{root,home}/<kind>_<ts> (SIN manifest) ----------------------------------


class RemoteStatus:
    NONE = "none"  # no hay remoto configurado
    UNREACHABLE = "unreachable"  # el ssh no conectó
    NO_PRIVILEGE = "no_privilege"  # conectó, pero sin `sudo btrfs`: solo lo legible sin root
    CONNECTED = "connected"


def remote_btrfs_command(base: str) -> str:
    """Un solo viaje por ssh que además dice POR QUÉ falló. Cada parte dinámica va con shlex.quote.
    `-r -R`: solo lectura + received_uuid => una copia recibida a medias no aparece."""
    q, dest = shlex.quote, base.rstrip("/")
    return "; ".join([
        "echo __CONN_OK__",
        f"if sudo -n btrfs subvolume list -r -R {q(dest)} 2>/dev/null; then echo __SUDO_OK__; "
        f"else echo __SUDO_NO__; "
        f"find {q(dest)}/root {q(dest)}/home -mindepth 1 -maxdepth 1 "
        f"-printf 'nosudo %p\\n' 2>/dev/null || true; fi",
    ])  # fmt: skip


def scan_remote_btrfs(
    *,
    host: str,
    user: str,
    port: int,
    base: str,
    label: str,
    local_user: str,
    home: Path,
    local_dir: Path,
    ssh_home: Optional[Path] = None,
    run: Callable = subprocess.run,
    timeout: int = 16,
) -> Tuple[List[Snapshot], str, int]:
    """(snapshots, estado, cuántos se ocultaron por estar ya en local). No escribe nada."""
    if not host or not base:
        return [], RemoteStatus.NONE, 0
    ident, known = user_ssh_files(ssh_home) if ssh_home else (None, None)
    argv = build_ssh_argv(f"{user}@{host}", port, identity=ident, known_hosts=known)
    argv.append(remote_btrfs_command(base))
    try:
        res = run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("remote scan of %s failed: %s", host, exc)
        return [], RemoteStatus.UNREACHABLE, 0
    if res.returncode != 0 or "__CONN_OK__" not in res.stdout:
        logger.warning(
            "remote scan of %s failed: %s",
            host,
            (res.stderr or "").strip() or f"exit {res.returncode}",
        )
        return [], RemoteStatus.UNREACHABLE, 0
    status = RemoteStatus.CONNECTED if "__SUDO_OK__" in res.stdout else RemoteStatus.NO_PRIVILEGE
    skipped = 0
    by_key: dict = {}
    base = base.rstrip("/")
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("__"):
            continue
        if line.startswith(
            "nosudo "
        ):  # sin sudo: solo nombres (no se puede verificar received_uuid)
            full = line[len("nosudo ") :].strip()
            name = full.rsplit("/", 1)[-1]
            seg = full.rsplit("/", 2)[-2] if full.count("/") >= 2 else ""
            rel = f"{seg}/{name}"
        elif "path " in line and has_received_uuid(line):
            rel = line.split("path ", 1)[1].strip()
            name = rel.split("/")[-1]
            full = f"{base}/{rel}"
        else:
            continue
        m = COMPACT_RE.match(name)
        kind = name.split("_", 1)[0]
        if not m or kind not in ("home", "root") or not rel.startswith(kind):
            continue
        if (local_dir / name).is_dir():  # ya está en local: no se ofrece una descarga de 30 GB
            skipped += 1
            continue
        key = key_from_compact(name.split("_", 1)[1])
        if not key:
            continue
        sub = by_key.setdefault(key, [])
        if kind == "home":
            sub.append(Area("home", "Home", full, home, Storage.REMOTE, True))
        else:
            sub.append(Area("root", "Root /", full, Path("/"), Storage.REMOTE))
    snaps = [
        Snapshot(k, parse_key_time(k) or datetime.now(), "completed" if status == RemoteStatus.CONNECTED else "unknown",
                 [Origin(label, "remote", areas, "btrfs")])
        for k, areas in by_key.items()
    ]  # fmt: skip
    return sorted(snaps, key=lambda s: s.timestamp, reverse=True), status, skipped
