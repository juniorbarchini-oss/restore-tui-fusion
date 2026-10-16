"""Interfaz del gestor de paquetes: QUÉ reinstalar (apt vs pacman/AUR), sin lógica de filesystem.

Cada gestor sabe: (1) capturar su estado en `_system_state/` (listas de paquetes, repos, y lo
auxiliar que cuelga de él: flatpak/snap/dconf o AUR/Omarchy/bootloader), (2) qué fragmentos de
bash componen su restore.sh y (3) cuáles forman la lista "software to install" del final: lo que
no se pudo reinstalar sin supervisión se entrega como lista, no como un error.
"""

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Tuple

CaptureFn = Callable[["CaptureContext"], None]

# directorios que nunca vale la pena copiar (se regeneran, son enormes o secretos)
COPY_SKIP = {"gnupg", "__pycache__", ".git"}


@dataclass
class Roots:
    """Raíces del sistema que se leen; en los tests apuntan a un árbol temporal."""

    etc: Path = Path("/etc")
    usr: Path = Path("/usr")
    boot: Path = Path("/boot")


@dataclass
class CaptureContext:
    meta_dir: Path
    warnings: List[str] = field(default_factory=list)
    roots: Roots = field(default_factory=Roots)
    run: Callable[..., subprocess.CompletedProcess] = None  # type: ignore[assignment]
    which: Callable[[str], object] = shutil.which

    def __post_init__(self):
        if self.run is None:
            self.run = lambda cmd, timeout=60: subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )

    def write(self, name: str, content: str) -> None:
        (self.meta_dir / name).write_text(content)

    def copy(self, src: Path, dst_subdir: str) -> None:
        """Copia un archivo o un árbol (sin symlinks ni COPY_SKIP) a `_system_state/<dst_subdir>`."""
        p = Path(src)
        if not p.exists():
            return
        target = self.meta_dir / dst_subdir
        target.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            for item in p.rglob("*"):
                if any(part in COPY_SKIP for part in item.parts):
                    continue
                if item.is_file() and not item.is_symlink():
                    try:
                        rel = item.relative_to(p)
                        (target / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target / rel)
                    except OSError as exc:
                        self.warnings.append(f"copy {item}: {exc}")
        else:
            try:
                shutil.copy2(p, target / p.name)
            except OSError as exc:
                self.warnings.append(f"copy {src}: {exc}")

    def write_json(self, name: str, data: dict) -> None:
        self.write(name, json.dumps(data, indent=2))


class PackageManager(ABC):
    id: str = "?"

    @classmethod
    @abstractmethod
    def detect(cls, os_release_text: str) -> bool:
        """¿Es este el gestor de la distro descrita por /etc/os-release?"""

    @abstractmethod
    def capture_steps(self) -> List[Tuple[str, CaptureFn]]:
        """(nombre, función) en orden; cada paso es independiente: uno que falla es un aviso."""

    @abstractmethod
    def etc_files(self) -> List[str]:
        """Archivos bajo /etc que se guardan en `_system_state/etc/`."""

    @abstractmethod
    def leftovers_fragment_ids(self) -> List[str]:
        """Fragmentos del restore.sh que forman la lista 'software to install'."""

    def restore_script(self, engine: str) -> str:
        from ..system_state.restore_script import compose

        return compose(self.id, engine)

    def leftovers_fragment(self, engine: str = "rsync") -> str:
        from ..system_state.restore_script import fragments_for, _read

        wanted = set(self.leftovers_fragment_ids())
        return "".join(
            _read(rel) for fid, rel, _n in fragments_for(self.id, engine) if fid in wanted
        )


def parse_pkg_lines(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]
