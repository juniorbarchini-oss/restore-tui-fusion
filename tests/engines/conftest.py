import datetime as _dt
import os
import stat
import textwrap
from pathlib import Path

import pytest

from restore_fusion.destinations import LocalDestination
from restore_fusion.engines import EngineContext
from restore_fusion.safety import ask_delete


class Clock:
    """Reloj falso para el módulo del motor: cada tick() avanza un día (nombres distintos)."""

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

    import restore_fusion.engines.rsync_hardlink as mod

    monkeypatch.setattr(mod, "datetime", FakeDT)
    return c


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "Documents").mkdir(parents=True)
    (h / ".cache" / "junk").mkdir(parents=True)
    (h / "Documents" / "a.txt").write_text("hello")
    (h / "Documents" / "b.txt").write_text("world")
    (h / ".cache" / "junk" / "trash.bin").write_text("x" * 1000)
    return h


@pytest.fixture
def stage(tmp_path):
    s = tmp_path / "stage"
    (s / "_system_state").mkdir(parents=True)
    (s / "_system_state" / "os_info.json").write_text("{}")
    (s / "restore.sh").write_text("#!/bin/sh\n")
    return s


@pytest.fixture
def ctx(home, stage):
    return EngineContext(
        source_home=home,
        user="tester",
        exclusions=[".cache/", "**/.cache/"],
        stage_dir=stage,
        hostname="host1",
    )


@pytest.fixture
def usb(tmp_path):
    (tmp_path / "usb").mkdir()
    return LocalDestination(tmp_path / "usb" / "restore-tui", is_mount=lambda p: True)


@pytest.fixture
def approve_all():
    def make(*names):
        return ask_delete(
            "d?", list(names), input_fn=lambda _p: "y", is_tty=lambda: True, print_fn=lambda s: None
        )

    return make
