"""apt (Debian/Ubuntu): paquetes manuales, repos y llaveros, + snap/flatpak/dconf/GNOME.
De restore-tui/system_state.py."""

import re
from typing import List, Tuple

from .base import CaptureContext, CaptureFn, PackageManager
from .desktop import capture_dconf, capture_gnome_extensions
from .flatpak import capture_flatpaks
from .snap import capture_snaps

# Hardware/kernel: reinstalarlos en otra máquina no sirve o daña; se sacan de la lista.
DRIVER_EXCLUDE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^nvidia-.*", r"^libnvidia-.*", r"^xserver-xorg-video-nvidia-.*",
        r"^cuda.*", r"^linux-.*", r"^intel-microcode.*", r"^amd64-microcode.*",
        r"^bcmwl-kernel-source.*", r"^broadcom-sta-.*", r"^firmware-.*",
        r"^alsa-firmware.*", r"^fglrx.*", r"^oem-.*-meta$",
    ]
]  # fmt: skip


def capture_apt_packages(ctx: CaptureContext) -> None:
    if not ctx.which("apt-mark"):
        return
    res = ctx.run(["apt-mark", "showmanual"])
    if res.returncode != 0:
        return
    pkgs = sorted(
        p
        for line in res.stdout.splitlines()
        if (p := line.strip()) and not any(pat.match(p) for pat in DRIVER_EXCLUDE_PATTERNS)
    )
    ctx.write("apt_packages_manual.txt", "\n".join(pkgs) + "\n")


def capture_apt_sources(ctx: CaptureContext) -> None:
    """Repos de terceros + sus llaveros. /usr/share/keyrings es donde la mayoría de los repos
    modernos guardan la llave que `Signed-By=` señala; sin ella apt descarta el repo en silencio.
    Los nombres se mapean explícito: /etc/apt/keyrings y /usr/share/keyrings terminan igual."""
    dest = ctx.meta_dir / "apt_sources"
    dirs = {
        ctx.roots.etc / "apt" / "sources.list.d": "sources.list.d",
        ctx.roots.etc / "apt" / "keyrings": "keyrings",
        ctx.roots.etc / "apt" / "trusted.gpg.d": "trusted.gpg.d",
        ctx.roots.usr / "share" / "keyrings": "usr_share_keyrings",
    }
    for src_dir, name in dirs.items():
        if not src_dir.is_dir():
            continue
        target = dest / name
        target.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            if item.is_file():
                try:
                    import shutil

                    shutil.copy2(item, target / item.name)
                except OSError as exc:
                    ctx.warnings.append(f"apt source {item.name}: {exc}")


class AptManager(PackageManager):
    id = "apt"

    @classmethod
    def detect(cls, os_release_text: str) -> bool:
        from ..detect import detect_package_manager

        return detect_package_manager(os_release_text) == "apt"

    def capture_steps(self) -> List[Tuple[str, CaptureFn]]:
        return [
            ("dconf", capture_dconf),
            ("gnome_extensions", capture_gnome_extensions),
            ("apt_packages", capture_apt_packages),
            ("apt_sources", capture_apt_sources),
            ("snaps", capture_snaps),
            ("flatpaks", capture_flatpaks),
        ]

    def etc_files(self) -> List[str]:
        return ["hosts", "environment", "hostname", "fstab"]

    def leftovers_fragment_ids(self) -> List[str]:
        return ["apt-packages", "snap", "symlinks-checklist", "footer-and-checklist"]
