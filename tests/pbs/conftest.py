"""Fixtures de PBS (el doble vive en tests/helpers/pbs_fakes.py)."""

import pytest
from pbs_fakes import SECRET, FakePbs

from restore_fusion.config.schema import Config


@pytest.fixture(autouse=True)
def _no_real_power_profiles(monkeypatch):
    """Hermético: que el equipo de pruebas tenga o no power-profiles-daemon no cambia el argv."""
    from restore_fusion.pbs import push

    monkeypatch.setattr(push, "power_saver_available", lambda *a, **k: False)


@pytest.fixture
def fake():
    return FakePbs()


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home" / "tester"
    home.mkdir(parents=True)
    return Config(
        user="tester",
        home=home,
        pbs_repository="u@pbs!tok@pbs.example:store",
        pbs_password=SECRET,
        pbs_fingerprint="AA:BB",
        local_snapshots_dir=tmp_path / "snaps",
        extra_exclusions=["Downloads/"],
    )
