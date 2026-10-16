"""Kit de recuperación de desastre: `disaster-recovery.sh` + `RECOVERY.md` + el código fuente de la
app, en la RAÍZ del destino (no dentro de un snapshot), tras cada backup completado.

Los scripts son bash autónomo (sin Python, sin esta app). Hay cuatro, uno por (motor, destino):
rsync/btrfs x local/remoto, tomados tal cual de las dos ediciones (`templates/`). Los .md usan
marcadores @@CLAVE@@ (nunca .format: una `{` suelta rompería el texto).
"""

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

TEMPLATES = Path(__file__).parent / "templates"
TARBALL_NAME = "restore-fusion-src.tar.gz"
_SRC_ROOT = Path(__file__).resolve().parents[2]
_MEMBERS = (
    "restore_fusion", "bin", "install.sh", "uninstall.sh", "requirements.txt",
    "config.conf.example", "README.md", "pyproject.toml",
)  # fmt: skip


def build_src_tarball(src_root: Path = _SRC_ROOT) -> bytes:
    """Código de esta app, para reinstalarla en una máquina nueva sin red ni GitHub. b'' si falla."""
    members = [m for m in _MEMBERS if (src_root / m).exists()]
    if not members:
        return b""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["tar", "czf", tmp_path, "--exclude=__pycache__", "--exclude=*.pyc", "-C", str(src_root), *members],
            capture_output=True, timeout=60, check=True,
        )  # fmt: skip
        return Path(tmp_path).read_bytes()
    except (subprocess.SubprocessError, OSError):
        return b""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _fill(text: str, values: Dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace(f"@@{k}@@", v)
    return text


def kit_files(
    engine: str,
    dest,
    *,
    latest: str,
    host: str,
    when: Optional[str] = None,
    tarball: Optional[bytes] = None,
) -> Dict[str, bytes]:
    """{ruta_relativa: contenido} para la raíz del destino."""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    remote = hasattr(dest, "ssh_argv")  # SSHDestination
    kind = f"{engine}_{'remote' if remote else 'local'}"
    values = {"HOST_NAME": host, "LATEST": latest, "WHEN": when}
    if remote:
        values.update(
            HOST=f"{dest.user}@{dest.host}", REMOTE=f"{dest.user}@{dest.host}", BASE=dest.root,
            PORT=str(dest.port),
        )  # fmt: skip
    script = _fill((TEMPLATES / f"{kind}.sh").read_text(encoding="utf-8"), values)
    md = _fill((TEMPLATES / f"{kind}.md").read_text(encoding="utf-8"), values)
    files = {"disaster-recovery.sh": script.encode(), "RECOVERY.md": md.encode("utf-8")}
    tar = build_src_tarball() if tarball is None else tarball
    if tar:
        files[TARBALL_NAME] = tar
    return files
