"""Carga de configuración: entorno > archivo > valor por defecto.

  1. Variable de entorno RESTORE_TUI_<CLAVE>   (BTRFS_<CLAVE> heredada, con aviso)
  2. Archivo ~/.config/restore-tui/config.conf, luego ~/.config/btrfs-restore/config.conf,
     luego /etc/restore-tui/ y /etc/btrfs-restore/
  3. Valor por defecto (schema.py)

`load()` SOLO LEE: nunca escribe, ni sondea unidades. El archivo solo lo modifica la
pantalla Settings (store.py). La unidad de destino se resuelve aparte, con
`resolve_target()`.
"""

import getpass
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional

from .. import drives
from .schema import Config

logger = logging.getLogger("restore_fusion")

ENV_PREFIX = "RESTORE_TUI_"

# Claves heredadas del archivo de btrfs-restore-tui -> clave canónica (con aviso).
LEGACY_FILE_KEYS = {
    "BTRFS_REMOTE_HOST": "REMOTE_HOST",
    "BTRFS_REMOTE_PATH": "REMOTE_PATH",
    "BTRFS_REMOTE_USER": "REMOTE_USER",
    "BTRFS_REMOTE_NAME": "REMOTE_NAME",
    "BTRFS_REMOTE_PORT": "REMOTE_PORT",
}

_WARNED: set = set()


def current_user(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get("SUDO_USER") or environ.get("USER") or getpass.getuser()


def home_of(user: str) -> Path:
    """Home real de `user`, también cuando corre bajo sudo."""
    try:
        import pwd

        return Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, ImportError):
        return Path(f"/home/{user}")


def config_candidates(home: Path, etc_dir: Path = Path("/etc")) -> List[Path]:
    return [
        home / ".config" / "restore-tui" / "config.conf",
        home / ".config" / "btrfs-restore" / "config.conf",
        etc_dir / "restore-tui" / "config.conf",
        etc_dir / "btrfs-restore" / "config.conf",
    ]


def read_config_file(candidates: List[Path]) -> dict:
    """Gana el primer archivo que exista. {} si no hay o no se puede leer."""
    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        values: dict = {}
        excludes: List[str] = []
        legacy_seen: List[str] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().upper()
                val = val.strip().strip('"').strip("'")
                if key in LEGACY_FILE_KEYS:
                    legacy_seen.append(key)
                    key = LEGACY_FILE_KEYS[key]
                if key == "EXCLUDE":
                    if val:
                        excludes.append(val)
                else:
                    values[key] = val
        except OSError:
            return {}
        if legacy_seen and str(path) not in _WARNED:
            _WARNED.add(str(path))
            logger.warning(
                "config %s uses deprecated keys %s - rename them to %s",
                path,
                ", ".join(sorted(set(legacy_seen))),
                ", ".join(sorted({LEGACY_FILE_KEYS[k] for k in legacy_seen})),
            )
        if excludes:
            values["_EXCLUDES"] = excludes
        values["_SOURCE"] = str(path)
        return values
    return {}


def _env(environ: Mapping[str, str], name: str) -> Optional[str]:
    """RESTORE_TUI_<NAME>, con BTRFS_<NAME> heredado como respaldo (deprecado)."""
    v = environ.get(ENV_PREFIX + name)
    if v not in (None, ""):
        return v
    legacy = environ.get("BTRFS_" + name)
    if legacy in (None, "") and name.startswith("REMOTE_"):
        legacy = environ.get("BTRFS_REMOTE_" + name[len("REMOTE_") :])
    if legacy not in (None, "") and f"env:{name}" not in _WARNED:
        _WARNED.add(f"env:{name}")
        logger.warning("env BTRFS_%s is deprecated - use %s%s", name, ENV_PREFIX, name)
    return legacy


def _int(*vals, lo: int = 0) -> Optional[int]:
    for v in vals:
        if v not in (None, ""):
            try:
                return max(lo, int(v))
            except (TypeError, ValueError):
                pass
    return None


def _falsy(v: Optional[str]) -> bool:
    return v is not None and str(v).strip().lower() in ("0", "false", "no", "off")


def load(
    user: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    etc_dir: Path = Path("/etc"),
) -> Config:
    environ = os.environ if environ is None else environ
    user = user or current_user(environ)
    home = home if home is not None else home_of(user)
    cfg = Config(user=user, home=home)
    fv = read_config_file(config_candidates(home, etc_dir))
    cfg.config_source = fv.get("_SOURCE", cfg.config_source)

    def get(name: str, *file_names: str) -> Optional[str]:
        v = _env(environ, name)
        if v not in (None, ""):
            return v
        for fn in file_names or (name,):
            if fv.get(fn) not in (None, ""):
                return fv[fn]
        return None

    eng = get("ENGINE")
    if eng:
        cfg.engine = eng.strip().lower()  # valor inválido: lo rechaza detect.choose_engine

    cfg.target_dir = _env(environ, "TARGET") or get("TARGET_DIR") or None
    cfg.target_label = get("TARGET_LABEL") or cfg.target_label

    pct = _int(get("MAX_DISK_PERCENT"), lo=10)
    if pct is not None:
        cfg.max_disk_percent = min(99, pct)
    mk = _int(get("MIN_KEEP"), lo=1)
    if mk is not None:
        cfg.min_keep = mk
    lk = _int(get("LOCAL_KEEP"), lo=1)
    if lk is not None:
        cfg.local_keep = lk
    rk = _int(get("REMOTE_KEEP"), lo=1)
    if rk is not None:
        cfg.remote_keep = rk
    cap = _int(_env(environ, "KEEP"), get("MAX_SNAPSHOTS", "MAX_SNAPSHOTS", "KEEP_SNAPSHOTS"))
    if cap is not None:
        cfg.max_snapshots = cap
    rgb = _int(get("REMOTE_MAX_GB"))
    if rgb is not None:
        cfg.remote_max_gb = rgb

    snaps = get("LOCAL_SNAPSHOTS_DIR")
    if snaps:
        cfg.local_snapshots_dir = Path(snaps)
    staging = get("STAGING_DIR")
    if staging:
        cfg.staging_dir = Path(staging)

    if _falsy(get("EXCLUDE_DEFAULTS")):
        cfg.exclude_defaults = False
    cfg.extra_exclusions = list(fv.get("_EXCLUDES", []))
    env_excl = _env(environ, "EXCLUDE")
    if env_excl:
        cfg.extra_exclusions += [p.strip() for p in env_excl.split(":") if p.strip()]

    # -- remoto: sin valores por defecto ---------------------------------
    if _falsy(get("REMOTE_ENABLED")):
        cfg.remote_enabled = False
    cfg.remote_host = get("REMOTE_HOST") or ""
    cfg.remote_host_lan = (get("REMOTE_HOST_LAN") or "").strip()
    cfg.remote_path = get("REMOTE_PATH") or ""
    cfg.remote_user = get("REMOTE_USER") or user
    port = _int(get("REMOTE_PORT"), lo=1)
    cfg.remote_port = port if port is not None else 22
    cfg.remote_name = get("REMOTE_NAME") or cfg.remote_host

    cfg.pbs_repository = get("PBS_REPOSITORY") or ""
    cfg.pbs_password = get("PBS_PASSWORD") or ""
    cfg.pbs_fingerprint = get("PBS_FINGERPRINT") or ""
    if _falsy(get("PBS_POWER_SAVER")):
        cfg.pbs_power_saver = False
    if _falsy(get("CHECK_UPDATES")):
        cfg.check_updates = False
    return cfg


@dataclass(frozen=True)
class TargetResolution:
    mount: Optional[Path]
    root: Optional[Path]  # <mount>/<carpeta del motor>
    source: str  # "config" | "autodetect" | "none"
    ok: bool
    reason: str


def resolve_target(
    cfg: Config,
    engine: str,
    *,
    bases=None,
    is_mount=drives._real_ismount,
    fstype=drives.fstype_of,
) -> TargetResolution:
    """Destino USB: TARGET_DIR explícito o autodetección conservadora. Siempre exige un
    punto de montaje real; sin fallback a "la primera unidad"."""
    if cfg.target_dir:
        source, target = "config", cfg.target_dir
    else:
        found = drives.autodetect_target(
            cfg.user, engine, cfg.target_label, bases=bases, is_mount=is_mount
        )
        if not found:
            return TargetResolution(
                None,
                None,
                "none",
                False,
                "no backup drive found: mount the right drive or set TARGET_DIR",
            )
        source, target = "autodetect", found
    ok, why = drives.validate_target(target, engine, is_mount=is_mount, fstype=fstype)
    mount, root = drives.split_target(target, engine)
    return TargetResolution(mount, root, source, ok, why)
