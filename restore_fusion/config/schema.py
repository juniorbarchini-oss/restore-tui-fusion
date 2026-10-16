"""Esquema de configuración: la unión de los dos esquemas (btrfs + ext4).

Sin ningún host, IP ni ruta por defecto (decisión 5): el usuario pone sus valores en su
propio config.conf. Los valores por defecto aquí son neutros (vacíos) o numéricos.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Retención (igual en ambas ediciones)
DEFAULT_MAX_DISK_PERCENT = 80
DEFAULT_MIN_KEEP = 2
DEFAULT_MAX_SNAPSHOTS = 0  # 0 = sin tope duro
DEFAULT_LOCAL_KEEP = 10  # snapshots RO locales (padres de `btrfs send -p`)
DEFAULT_REMOTE_MAX_GB = 200  # presupuesto del árbol remoto; 0 = sin tope

DEFAULT_SOURCE_MOUNTS = ["/", "/home"]  # solo motor btrfs
DEFAULT_LOCAL_SNAPSHOTS_DIR = Path("/.snapshots")  # solo motor btrfs
DEFAULT_TARGET_LABEL = "USB_DATA"

SNAPSHOT_NAME_LEN = 17  # YYYY-MM-DD_HHMMSS


@dataclass
class Config:
    user: str = ""
    home: Path = Path("/")  # home real del usuario (también bajo sudo)
    config_source: str = "(defaults)"

    engine: str = "auto"  # auto | rsync | btrfs (se valida en detect.choose_engine)

    # -- destino USB / directorio --------------------------------------
    target_dir: Optional[str] = None  # explícito (env/archivo); None = autodetectar
    target_label: str = DEFAULT_TARGET_LABEL

    # -- retención ------------------------------------------------------
    max_disk_percent: int = DEFAULT_MAX_DISK_PERCENT
    min_keep: int = DEFAULT_MIN_KEEP
    max_snapshots: int = DEFAULT_MAX_SNAPSHOTS
    local_keep: int = DEFAULT_LOCAL_KEEP
    remote_max_gb: int = DEFAULT_REMOTE_MAX_GB
    # Copias que se guardan en cada destino SSH (motor btrfs). None = igual que local_keep.
    remote_keep: Optional[int] = None

    # -- solo motor btrfs ----------------------------------------------
    source_mounts: List[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_MOUNTS))
    local_snapshots_dir: Path = DEFAULT_LOCAL_SNAPSHOTS_DIR
    staging_dir: Optional[Path] = None

    # -- exclusiones ----------------------------------------------------
    # Las listas integradas viven en cada motor (rsync y btrfs usan dialectos distintos).
    exclude_defaults: bool = True
    extra_exclusions: List[str] = field(default_factory=list)

    # -- remoto SSH (todo vacío por defecto) ---------------------------
    remote_enabled: bool = True
    remote_host: str = ""
    remote_host_lan: str = ""
    remote_path: str = ""
    remote_user: str = ""
    remote_port: int = 22
    remote_name: str = ""

    # -- Proxmox Backup Server -----------------------------------------
    pbs_repository: str = ""
    pbs_password: str = field(default="", repr=False)  # nunca en repr/logs
    pbs_fingerprint: str = ""
    pbs_power_saver: bool = True  # el push corre con perfil power-saver (si el sistema lo ofrece)

    @property
    def effective_remote_keep(self) -> int:
        """REMOTE_KEEP si está definido; si no, LOCAL_KEEP (configs existentes no cambian)."""
        return self.local_keep if self.remote_keep is None else self.remote_keep

    @property
    def remote_configured(self) -> bool:
        return bool(self.remote_enabled and self.remote_host and self.remote_path)

    def remote_root(self, engine: str) -> Optional[str]:
        """Raíz del backup en el remoto. rsync usa `<ruta>/restore-tui`; btrfs usa la ruta tal cual
        (`<ruta>/{root,home,meta}`), como las dos ediciones por separado."""
        if not self.remote_configured:
            return None
        root = self.remote_path.rstrip("/")
        if engine == "rsync" and not root.endswith("restore-tui"):
            root = f"{root}/restore-tui"
        return root
