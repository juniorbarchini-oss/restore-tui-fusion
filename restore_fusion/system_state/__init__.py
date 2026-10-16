"""Captura de estado del sistema y generación de restore.sh."""

from .collector import SystemStateCollector
from .restore_script import compose, strip_markers

__all__ = ["SystemStateCollector", "compose", "strip_markers"]
