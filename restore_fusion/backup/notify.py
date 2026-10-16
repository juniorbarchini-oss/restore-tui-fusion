"""Alertas de escritorio (mejor esfuerzo) para backup-now. Reuso de btrfs-restore-tui/notify.py.

backup-now corre como root (sudo) o desde un timer, así que la notificación se entrega al bus de
sesión del usuario real. Nunca lanza: una alerta que no se puede mostrar no debe convertir un
backup terminado en un crash. La misma alerta no se repite dentro de REPEAT segundos y el estado
se limpia en cuanto un backup termina bien.
"""

import hashlib
import os
import pwd
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

REPEAT = 3600


def _bus_path(uid: int) -> str:
    return f"/run/user/{uid}/bus"


def send_alert(
    title: str,
    body: str,
    user: str,
    state_file: Optional[Path],
    run: Callable = subprocess.run,
    now: Callable = time.time,
    exists: Callable = os.path.exists,
    is_root: Optional[bool] = None,
) -> bool:
    """Muestra una notificación crítica a `user`. True si se envió."""
    try:
        key = hashlib.md5(f"{title}\n{body}".encode()).hexdigest()
        if state_file is not None:
            try:
                last_key, last_ts = state_file.read_text().split()
                if last_key == key and now() - float(last_ts) < REPEAT:
                    return False
            except (OSError, ValueError):
                pass
        root = (os.geteuid() == 0) if is_root is None else is_root
        cmd = ["notify-send", "-u", "critical", "-a", "backup-now", title, body[:300]]
        if root:
            uid = pwd.getpwnam(user).pw_uid
            bus = _bus_path(uid)
            if not exists(bus):
                return False  # ese usuario no tiene sesión gráfica
            cmd = [
                "runuser",
                "-u",
                user,
                "--",
                "env",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}",
                *cmd,
            ]
        res = run(cmd, capture_output=True, timeout=10)
        if res.returncode != 0:
            return False
        if state_file is not None:
            try:
                state_file.write_text(f"{key} {now()}")
            except OSError:
                pass
        return True
    except (OSError, KeyError, subprocess.SubprocessError):
        return False


def clear_alert(state_file: Optional[Path]) -> None:
    try:
        if state_file is not None:
            state_file.unlink()
    except OSError:
        pass
