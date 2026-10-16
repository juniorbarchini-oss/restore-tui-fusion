"""AUR (Arch): paquetes foráneos. `pacman -Qqe` también lista los de AUR, por eso la lista de
paquetes de repo EXCLUYE los foráneos: el `pacman -S` del restore.sh no debe chocar con
"target not found" por un nombre que solo conoce AUR. El helper (yay/paru) lo elige restore.sh."""

from typing import Set

from .base import CaptureContext


def foreign_packages(ctx: CaptureContext) -> Set[str]:
    res = ctx.run(["pacman", "-Qqm"])
    if res.returncode != 0:
        return set()
    ctx.write("pkglist_aur.txt", res.stdout)
    return set(res.stdout.split())
