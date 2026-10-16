import subprocess

import pytest

from restore_fusion.packages import AptManager, PacmanManager, Roots
from restore_fusion.system_state import SystemStateCollector


@pytest.fixture
def snap(tmp_path):
    s = tmp_path / "2026-09-08_120000"
    s.mkdir()
    return s


@pytest.fixture
def roots(tmp_path):
    r = tmp_path / "sysroot"
    for d in ("etc", "usr/local/bin", "usr/share", "boot"):
        (r / d).mkdir(parents=True)
    return Roots(etc=r / "etc", usr=r / "usr", boot=r / "boot")


def fake_run_factory(table):
    """table: {tuple(prefix): stdout}. Todo lo demás devuelve éxito con salida vacía."""

    def run(cmd, timeout=60):
        for prefix, out in table.items():
            if list(cmd[: len(prefix)]) == list(prefix):
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


@pytest.fixture
def make_collector(snap, roots):
    def make(pm="apt", engine="rsync", run=None, which=lambda n: None):
        mgr = AptManager() if pm == "apt" else PacmanManager()
        return SystemStateCollector(
            snap, mgr, engine, roots=roots, run=run or fake_run_factory({}), which=which
        )

    return make
