"""pacman (Arch/Omarchy): paquetes explícitos (repo) y de AUR, config de pacman, + flatpak,
systemd, Omarchy y bootloader. De btrfs-restore-tui/system_state.py."""

import re
from typing import List, Tuple

from .aur import foreign_packages
from .base import CaptureContext, CaptureFn, PackageManager
from .desktop import capture_bootloader, capture_omarchy, capture_systemd
from .flatpak import capture_flatpaks

# Hardware/kernel: se sacan de la lista explícita
DRIVER_RE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^nvidia(-.*)?$", r"^lib32-nvidia.*", r"^nvidia-utils$", r"^opencl-nvidia$",
        r"^linux(-.*)?$", r"^linux-firmware.*", r"^amd-ucode$", r"^intel-ucode$",
        r"^broadcom-wl.*", r"^b43-.*", r"^mesa$", r"^vulkan-radeon$", r"^vulkan-intel$",
    )
]  # fmt: skip


def capture_pacman_packages(ctx: CaptureContext) -> None:
    if not ctx.which("pacman"):
        ctx.warnings.append("pacman not found; package lists not saved")
        return
    aur = foreign_packages(ctx)
    res = ctx.run(["pacman", "-Qqe"])
    if res.returncode == 0:
        # `-Qqe` incluye los de AUR (también son "explícitos"): esta lista es SOLO de repo
        pkgs = sorted(
            p
            for line in res.stdout.splitlines()
            if (p := line.strip()) and p not in aur and not any(rx.match(p) for rx in DRIVER_RE)
        )
        ctx.write("pkglist_explicit.txt", "\n".join(pkgs) + "\n")
    res_all = ctx.run(["pacman", "-Q"])
    if res_all.returncode == 0:
        ctx.write("pkglist_all_versions.txt", res_all.stdout)


def capture_pacman_config(ctx: CaptureContext) -> None:
    ctx.copy(ctx.roots.etc / "pacman.conf", "pacman")
    ctx.copy(ctx.roots.etc / "pacman.d", "pacman/pacman.d")


class PacmanManager(PackageManager):
    id = "pacman"

    @classmethod
    def detect(cls, os_release_text: str) -> bool:
        from ..detect import detect_package_manager

        return detect_package_manager(os_release_text) == "pacman"

    def capture_steps(self) -> List[Tuple[str, CaptureFn]]:
        return [
            ("pacman_packages", capture_pacman_packages),
            ("pacman_config", capture_pacman_config),
            ("systemd", capture_systemd),
            ("flatpaks", capture_flatpaks),
            ("omarchy", capture_omarchy),
            ("bootloader", capture_bootloader),
        ]

    def etc_files(self) -> List[str]:
        return ["hosts", "hostname", "fstab", "environment", "locale.conf", "vconsole.conf",
                "mkinitcpio.conf", "systemd/timesyncd.conf", "systemd/logind.conf"]  # fmt: skip

    def leftovers_fragment_ids(self) -> List[str]:
        return ["leftovers-init", "leftovers-packages", "leftovers-flatpak", "leftovers-report"]
