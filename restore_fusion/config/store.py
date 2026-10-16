"""Escritura de config.conf: SOLO la usa la pantalla Settings.

Edita en el lugar: las líneas existentes (comentarios, EXCLUDE=, claves ajenas) no se
tocan; la clave pedida se reescribe en su línea y las nuevas se añaden al final. El
archivo queda con modo 600 (puede llevar PBS_PASSWORD).
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional

from .loader import config_candidates, current_user, home_of

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def settings_file_path(home: Path, etc_dir: Path = Path("/etc")) -> Path:
    """El primer candidato que exista; si no, el canónico ~/.config/restore-tui/config.conf."""
    cands = config_candidates(home, etc_dir)
    for path in cands:
        if path.is_file():
            return path
    return cands[0]


def save_user_settings(
    updates: Dict[str, object],
    *,
    user: Optional[str] = None,
    home: Optional[Path] = None,
    etc_dir: Path = Path("/etc"),
) -> Path:
    for key, val in updates.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid config key: {key!r}")
        if "\n" in str(val) or "\r" in str(val):
            raise ValueError(f"newline in value for {key}")

    user = user or current_user()
    home = home if home is not None else home_of(user)
    path = settings_file_path(home, etc_dir)

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().upper()
            if key in remaining:
                out_lines.append(f"{key}={remaining.pop(key)}")
                continue
        out_lines.append(line)

    if remaining:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# --- written by the Settings screen ---")
        for key, val in remaining.items():
            out_lines.append(f"{key}={val}")

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # escritura atómica en el mismo directorio: un corte no deja el archivo a medias
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.conf.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
