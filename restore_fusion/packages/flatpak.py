"""Flatpak: lista de aplicaciones instaladas (compartido por apt y pacman)."""

from .base import CaptureContext


def capture_flatpaks(ctx: CaptureContext) -> None:
    if ctx.which("flatpak"):
        res = ctx.run(["flatpak", "list", "--app", "--columns=application"])
        if res.returncode == 0:
            ctx.write("flatpak_list.txt", res.stdout)
