"""Exclusiones del motor btrfs: globs relativos a cada punto de montaje origen.

`btrfs send` no puede saltarse rutas a mitad del stream: el motor toma un snapshot ESCRIBIBLE,
borra estas rutas dentro del snapshot (nunca del sistema vivo), lo pasa a solo lectura y envía
ese. Lista tomada de btrfs-restore-tui/config.py (DEFAULT_BTRFS_EXCLUSIONS).
"""

from typing import Dict, List

# Estrechas a propósito: nunca un directorio que contenga el punto de montaje de un subvolumen
# anidado (p. ej. /var/cache/pacman/pkg, /var/log ya son subvolúmenes propios en Omarchy).
DEFAULT_BTRFS_EXCLUSIONS: Dict[str, List[str]] = {
    "/": [
        "var/tmp/*",
        "var/crash/*",
        "var/lib/systemd/coredump/*",
    ],
    "/home": [
        "*/.cache",
        "*/.local/share/Trash",
        "*/.thumbnails",
        "*/.mozilla/firefox/*/cache2",
        "*/.var/app/*/cache",
        # Familia Chromium (Chrome, Chromium, apps Electron como VSCode): los *Cache por perfil.
        "*/.config/*/*/Cache",
        "*/.config/*/*/Code Cache",
        "*/.config/*/*/GPUCache",
        "*/.config/*/*/DawnCache",
        "*/.config/*/*/DawnGraphiteCache",
        "*/.config/*/*/DawnWebGPUCache",
        "*/.config/*/*/GrShaderCache",
        "*/.config/*/*/ShaderCache",
        "*/.config/*/*/Service Worker/CacheStorage",
        "*/.config/*/*/Service Worker/ScriptCache",
        "*/.config/*/Cache",
        "*/.config/*/CachedData",
        "*/.config/*/Code Cache",
        "*/.config/*/GPUCache",
        "*/.config/*/GrShaderCache",
        "*/.config/*/ShaderCache",
        "*/.config/*/component_crx_cache",
        "*/.config/*/DawnCache",
    ],
}


def exclusions_for(mount: str, exclude_defaults: bool, extra: List[str]) -> List[str]:
    """Lista integrada del mount (si no se apagó) + las EXCLUDE del usuario, sin duplicados."""
    pats: List[str] = []
    if exclude_defaults:
        pats += DEFAULT_BTRFS_EXCLUSIONS.get(mount, [])
    pats += extra
    seen: set = set()
    return [p for p in pats if not (p in seen or seen.add(p))]
