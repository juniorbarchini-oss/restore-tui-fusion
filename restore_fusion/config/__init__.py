"""Configuración unificada (unión de los esquemas de btrfs-restore-tui y restore-tui)."""

from .loader import TargetResolution, load, resolve_target
from .schema import Config
from .store import save_user_settings

__all__ = ["Config", "TargetResolution", "load", "resolve_target", "save_user_settings"]
