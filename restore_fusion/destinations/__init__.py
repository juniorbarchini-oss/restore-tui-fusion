"""Destinos: dónde vive el árbol de snapshots (transporte + metadatos, sin lógica de copia)."""

from .base import Destination, RemoteQueryError
from .local import LocalDestination
from .ssh import SSHDestination, build_ssh_argv

__all__ = [
    "Destination",
    "RemoteQueryError",
    "LocalDestination",
    "SSHDestination",
    "build_ssh_argv",
]
