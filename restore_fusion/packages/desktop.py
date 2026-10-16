"""Piezas de escritorio/sistema que cuelgan del gestor de paquetes: GNOME (dconf, extensiones),
y para Arch/Omarchy: systemd, Omarchy, bootloader."""

import os
from pathlib import Path

from .base import CaptureContext


def capture_dconf(ctx: CaptureContext) -> None:
    if not ctx.which("dconf"):
        ctx.warnings.append("dconf not found; GNOME settings not saved")
        return
    res = ctx.run(["dconf", "dump", "/"])
    if res.returncode == 0:
        ctx.write("dconf_dump.ini", res.stdout)


def capture_gnome_extensions(ctx: CaptureContext) -> None:
    """Qué extensiones de GNOME Shell estaban habilitadas. Su código viaja con $HOME; lo que no
    viaja es que GNOME las reactive (pide un login nuevo): restore.sh solo lo recuerda."""
    if not ctx.which("gnome-extensions"):
        return
    res = ctx.run(["gnome-extensions", "list", "--enabled"])
    if res.returncode == 0 and res.stdout.strip():
        ctx.write("gnome_extensions_enabled.txt", res.stdout)


def capture_systemd(ctx: CaptureContext) -> None:
    r = ctx.run(["systemctl", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"])
    if r.returncode == 0:
        ctx.write("systemd_enabled.txt", r.stdout)
    user = os.getenv("SUDO_USER")
    if user:
        ru = ctx.run(["runuser", "-u", user, "--", "systemctl", "--user", "list-unit-files",
                      "--state=enabled", "--no-legend", "--no-pager"])  # fmt: skip
        if ru.returncode == 0 and ru.stdout.strip():
            ctx.write("systemd_enabled_user.txt", ru.stdout)


def capture_omarchy(ctx: CaptureContext) -> None:
    info = {}
    ver = ctx.roots.usr / "share" / "omarchy" / "version"
    if ver.is_file():
        info["omarchy_version"] = ver.read_text().strip()
    if ctx.which("omarchy-version"):
        r = ctx.run(["omarchy-version"])
        if r.returncode == 0:
            info["omarchy_version"] = r.stdout.strip()
    info["note"] = "omarchy user config lives under $HOME/.config and is in the home snapshot"
    ctx.write_json("omarchy.json", info)


def capture_bootloader(ctx: CaptureContext) -> None:
    b, e = ctx.roots.boot, ctx.roots.etc
    found = {}
    for name, paths in {
        "limine": [b / "limine.conf", e / "limine.conf", b / "EFI" / "limine"],
        "grub": [e / "default" / "grub", b / "grub" / "grub.cfg"],
        "systemd-boot": [b / "loader" / "loader.conf", b / "loader" / "entries"],
    }.items():
        for p in paths:
            if Path(p).exists():
                found.setdefault(name, []).append(str(p))
                ctx.copy(p, f"bootloader/{name}")
    ctx.write_json("bootloader.json", found)
