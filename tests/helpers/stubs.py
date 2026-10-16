"""Dobles de prueba compartidos."""

from restore_fusion.packages.base import PackageManager


class StubPM(PackageManager):
    """Gestor de paquetes mínimo: sin capturas reales del sistema donde corre el test."""

    id = "apt"

    @classmethod
    def detect(cls, t):
        return True

    def capture_steps(self):
        return []

    def etc_files(self):
        return []

    def leftovers_fragment_ids(self):
        return []

    def restore_script(self, engine):
        return "#!/usr/bin/env bash\ntrue\n"
