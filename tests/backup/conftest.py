import datetime as _dt

import pytest

from restore_fusion.config.schema import Config
from restore_fusion.destinations import LocalDestination, SSHDestination


class Clock:
    def __init__(self):
        self.now_value = _dt.datetime(2026, 3, 1, 10, 0, 0)

    def tick(self, days=1):
        self.now_value += _dt.timedelta(days=days)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()

    class FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now_value

    import restore_fusion.engines.btrfs.engine as be
    import restore_fusion.engines.rsync_hardlink as re_

    monkeypatch.setattr(re_, "datetime", FakeDT)
    monkeypatch.setattr(be, "datetime", FakeDT)
    return c


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "Documents").mkdir(parents=True)
    (h / "Documents" / "a.txt").write_text("hello")
    return h


@pytest.fixture
def cfg(tmp_path, home):
    return Config(
        user="tester",
        home=home,
        local_snapshots_dir=tmp_path / "dot_snapshots",
        min_keep=2,
        max_snapshots=0,
    )


@pytest.fixture
def usb(tmp_path):
    (tmp_path / "usb").mkdir()
    return LocalDestination(tmp_path / "usb" / "restore-tui", is_mount=lambda p: True, label="USB")


@pytest.fixture
def ssh_dest(tmp_path):
    return SSHDestination(
        host="h.example",
        root=str(tmp_path / "remote" / "restore-tui"),
        user="u",
        label="Remote",
        tcp_check=lambda h, p: True,
    )
