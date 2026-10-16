"""Compone el restore.sh de recuperación bare-metal a partir de FRAGMENTOS.

restore.sh sigue siendo bash autónomo: solo bash, coreutils, rsync y el gestor de paquetes
(+ btrfs-progs/zstd si el snapshot es btrfs). Sin Python, sin esta app, sin red hacia ella.

Los fragmentos viven en `templates/<apt|pacman>/*.sh`. Los de las dos ediciones originales son
cortes EXACTOS de los restore.sh que generan hoy restore-tui y btrfs-restore-tui: la
composición (apt+rsync) y (pacman+btrfs) reproduce byte a byte lo que generaban, salvo las
marcas y los bloques nuevos:

    # @@fragment <id>                 marca antes de cada fragmento (salvo el primero)
    # @@new-begin <id> ... # @@new-end <id>   bloque NUEVO (no existía en la fuente)

`strip_markers()` quita ambas cosas; los tests de oro comparan el resultado con la fuente.
Los bloques nuevos son: la lista "SOFTWARE TO INSTALL" para pacman (la edición btrfs no la
tenía), la reinstalación de restore-fusion y las piezas de las combinaciones cruzadas
(apt+btrfs, pacman+rsync).
"""

import re
from pathlib import Path
from typing import List, Tuple

TEMPLATES = Path(__file__).parent / "templates"

# (id del fragmento, archivo bajo templates/, es_nuevo)
Frag = Tuple[str, str, bool]

_APT_BEFORE_HOME: List[Frag] = [
    ("header", "apt/00_header.sh", False),
    ("apt-sources", "apt/10_apt_sources.sh", False),
    ("apt-packages", "apt/20_apt_packages.sh", False),
    ("snap", "apt/30_snap.sh", False),
    ("flatpak", "apt/40_flatpak.sh", False),
]
_APT_AFTER_HOME: List[Frag] = [
    ("dconf", "apt/60_dconf.sh", False),
    ("usr-local-bin", "apt/70_usr_local_bin.sh", False),
    ("self-reinstall", "apt/80_self_reinstall.sh", False),
    ("fused-reinstall", "apt/85_fused_reinstall.sh", True),
    ("symlinks-checklist", "apt/90_symlinks.sh", False),
    ("crontab", "apt/95_crontab.sh", False),
    ("footer-and-checklist", "apt/99_footer.sh", False),
]
_APT_HOME = {
    "rsync": [("home-rsync", "apt/50_home_rsync.sh", False)],
    "btrfs": [("home-btrfs", "apt/50_home_btrfs.sh", True)],
}

_PAC_HEAD: List[Frag] = [
    ("preamble", "pacman/00_preamble.sh", False),
    ("leftovers-init", "pacman/02_leftovers_init.sh", True),
]
_PAC_HOME_DETECT = {
    "btrfs": [("home-detect-btrfs", "pacman/05_home_detect_btrfs.sh", False)],
    "rsync": [("home-detect-rsync", "pacman/05_home_detect_rsync.sh", True)],
}
_PAC_MID: List[Frag] = [
    ("banner-confirm", "pacman/10_banner_confirm.sh", False),
    ("pacman-packages", "pacman/20_packages.sh", False),
    ("leftovers-packages", "pacman/25_leftovers_packages.sh", True),
    ("flatpak", "pacman/30_flatpak.sh", False),
    ("leftovers-flatpak", "pacman/35_leftovers_flatpak.sh", True),
]
_PAC_HOME = {
    "btrfs": [("home-btrfs", "pacman/40_home_btrfs.sh", False)],
    "rsync": [("home-rsync", "pacman/40_home_rsync.sh", True)],
}
_PAC_TAIL: List[Frag] = [
    ("system-config", "pacman/50_system_config.sh", False),
    ("fused-reinstall", "pacman/60_fused_reinstall.sh", True),
    ("footer", "pacman/99_footer.sh", False),
    ("leftovers-report", "pacman/995_leftovers_report.sh", True),
]

ENGINES = ("rsync", "btrfs")
PACKAGE_MANAGERS = ("apt", "pacman")


def fragments_for(pm: str, engine: str) -> List[Frag]:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine!r}")
    if pm == "apt":
        return [*_APT_BEFORE_HOME, *_APT_HOME[engine], *_APT_AFTER_HOME]
    if pm == "pacman":
        return [*_PAC_HEAD, *_PAC_HOME_DETECT[engine], *_PAC_MID, *_PAC_HOME[engine], *_PAC_TAIL]
    raise ValueError(f"unknown package manager: {pm!r}")


def _read(rel: str) -> str:
    return (TEMPLATES / rel).read_text(encoding="utf-8")


def compose(pm: str, engine: str) -> str:
    out: List[str] = []
    for i, (fid, rel, is_new) in enumerate(fragments_for(pm, engine)):
        text = _read(rel)
        if not text.endswith("\n"):
            text += "\n"
        if is_new:
            out.append(f"# @@new-begin {fid}\n{text}# @@new-end {fid}\n")
        elif i:
            out.append(f"# @@fragment {fid}\n{text}")
        else:
            out.append(text)  # el shebang tiene que ser la primera línea
    return "".join(out)


_NEW_BLOCK = re.compile(r"^# @@new-begin (\S+)\n.*?^# @@new-end \1\n", re.S | re.M)
_MARK = re.compile(r"^# @@fragment \S+\n", re.M)


def strip_markers(script: str) -> str:
    """Quita las marcas y los bloques nuevos: lo que queda es el script de la edición original."""
    return _MARK.sub("", _NEW_BLOCK.sub("", script))


def leftovers_checklist_present(script: str) -> bool:
    """Las dos distros deben terminar con la lista "software to install"."""
    return all(s in script for s in ("TODO=()", "TODO+=(", "SOFTWARE TO INSTALL"))
