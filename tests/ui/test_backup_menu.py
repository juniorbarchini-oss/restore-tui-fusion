"""Menú de lanzamiento de backup-now con Textual Pilot (sin terminal real).
Portado de restore-tui/tests/test_tui_backup.py. CAMBIO de comportamiento respecto al original:
ningún destino viene marcado (regla 5 del README); el original marcaba todo lo alcanzable."""

import asyncio

import pytest

from restore_fusion.config.schema import Config
from restore_fusion.destinations import LocalDestination, SSHDestination

pytestmark = pytest.mark.needs_textual


class DeadRemote(SSHDestination):
    def reachable(self):
        return False, "host down (test)"


def cfg_for(tmp_path):
    c = Config(
        user="tester",
        home=tmp_path / "home",
        remote_host="h.example",
        remote_path="/r",
        remote_host_lan="lan.example",
    )
    return c


def dests(tmp_path, dead_remote=False):
    (tmp_path / "usb").mkdir(exist_ok=True)
    usb = LocalDestination(tmp_path / "usb" / "restore-tui", is_mount=lambda p: True, label="USB")
    cls = DeadRemote if dead_remote else SSHDestination
    rem = cls(
        host="h.example",
        host_lan="lan.example",
        root="/r/restore-tui",
        user="u",
        label="Box",
        tcp_check=lambda h, p: True,
    )
    if not dead_remote:
        rem.reachable = lambda: (True, "ok")
    return usb, rem


async def settle(pilot, n=30):
    for _ in range(n):
        await pilot.pause(0.1)
        if not pilot.app.workers._workers:
            break
    await pilot.pause(0.15)


def run(coro):
    return asyncio.run(coro)


def test_nothing_is_preselected_after_probing(tmp_path):
    from textual.widgets import Checkbox
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, rem = dests(tmp_path)
        app = BackupMenuApp(cfg_for(tmp_path), [usb, rem])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            for i in (0, 1):
                cb = app.query_one(f"#dest_{i}", Checkbox)
                assert not cb.disabled, "a reachable destination must be selectable"
                assert cb.value is False, "nothing may be pre-ticked"
            assert app.query_one("#opt_dryrun", Checkbox).value is False
            await pilot.press("b")  # Start sin marcar nada
            await pilot.pause(0.2)
        assert app.result is None, "Start with nothing ticked must not produce a backup"

    run(go())


def test_start_with_nothing_ticked_says_so(tmp_path):
    from textual.widgets import Label
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, rem = dests(tmp_path)
        app = BackupMenuApp(cfg_for(tmp_path), [usb, rem])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("b")
            await pilot.pause(0.2)
            assert "Nothing selected" in str(app.query_one("#status_bar", Label).render())

    run(go())


def test_ticked_destinations_dry_run_retention_and_route(tmp_path):
    from textual.widgets import Checkbox, RadioSet
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, rem = dests(tmp_path)
        cfg = cfg_for(tmp_path)
        app = BackupMenuApp(cfg, [usb, rem])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.query_one("#dest_0", Checkbox).value = True
            app.query_one("#dest_1", Checkbox).value = True
            assert app.query_one("#route", RadioSet).pressed_index == 0  # auto
            app.query_one("#route_wan").value = True
            await pilot.pause(0.2)
            app.query_one("#opt_dryrun", Checkbox).value = True
            app.query_one("#in_gb").value = "50"
            await pilot.pause(0.1)
            await pilot.press("b")
            await pilot.pause(0.2)
        r = app.result
        assert r is not None
        assert {d.label for d in r["destinations"]} == {"USB", "Box"}
        assert r["dry_run"] is True and r["remote_max_gb"] == 50 and r["remote_route"] == "wan"
        assert rem._candidates == ["h.example"], "route not pinned on the destination"

    run(go())


def test_only_the_ticked_destination_is_returned(tmp_path):
    from textual.widgets import Checkbox
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, rem = dests(tmp_path)
        app = BackupMenuApp(cfg_for(tmp_path), [usb, rem])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.query_one("#dest_1", Checkbox).value = True
            await pilot.press("b")
            await pilot.pause(0.2)
        assert [d.label for d in app.result["destinations"]] == ["Box"]

    run(go())


def test_unreachable_destination_is_disabled_and_cannot_be_chosen(tmp_path):
    from textual.widgets import Checkbox
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, rem = dests(tmp_path, dead_remote=True)
        app = BackupMenuApp(cfg_for(tmp_path), [usb, rem])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            cb = app.query_one("#dest_1", Checkbox)
            assert cb.disabled and not cb.value and "unreachable" in str(cb.label)
            app.query_one("#dest_0", Checkbox).value = True
            await pilot.press("b")
            await pilot.pause(0.2)
        assert [d.label for d in app.result["destinations"]] == ["USB"]

    run(go())


def test_unmounted_usb_is_disabled(tmp_path):
    from textual.widgets import Checkbox
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        (tmp_path / "usb").mkdir()
        usb = LocalDestination(
            tmp_path / "usb" / "restore-tui", is_mount=lambda p: False, label="USB"
        )
        app = BackupMenuApp(cfg_for(tmp_path), [usb])
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            cb = app.query_one("#dest_0", Checkbox)
            assert cb.disabled and not cb.value and "not a mount point" in str(cb.label)

    run(go())


def test_quit_returns_none(tmp_path):
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        usb, _ = dests(tmp_path)
        app = BackupMenuApp(cfg_for(tmp_path), [usb])
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("q")
        assert app.result is None

    run(go())


def test_no_candidates_says_so(tmp_path):
    from textual.widgets import Label
    from restore_fusion.ui.backup_menu import BackupMenuApp

    async def go():
        app = BackupMenuApp(cfg_for(tmp_path), [])
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            assert "No destination configured" in str(app.query_one("#status_bar", Label).render())

    run(go())
