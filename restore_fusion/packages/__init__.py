"""Gestores de paquetes (qué reinstalar): apt, pacman (+AUR), con flatpak/snap/desktop de apoyo."""

from typing import Optional

from .apt import AptManager
from .base import CaptureContext, PackageManager, Roots
from .pacman import PacmanManager

MANAGERS = {"apt": AptManager, "pacman": PacmanManager}


def manager_for(pm_id: Optional[str]) -> Optional[PackageManager]:
    cls = MANAGERS.get(pm_id or "")
    return cls() if cls else None


__all__ = [
    "AptManager",
    "PacmanManager",
    "PackageManager",
    "CaptureContext",
    "Roots",
    "manager_for",
]
