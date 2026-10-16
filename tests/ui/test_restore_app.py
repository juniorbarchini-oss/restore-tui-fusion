"""Pantalla de restauración de dos paneles con Textual Pilot (servicios simulados).
Reglas del README bajo prueba: nada preseleccionado; la confirmación no tiene botón por defecto;
el resultado es honesto ("Restored with errors"); no se sobrescribe un .bak anterior."""

import asyncio
import shutil
from pathlib import Path
from unittest import mock

import pytest
from restore_fakes import FakeServices, home_snapshot, remote_snapshot, two_area_snapshot

from restore_fusion.restore.engine import RestoreEngine

pytestmark = pytest.mark.needs_textual

K1, K2, K3 = "2026-09-08_100000", "2026-09-09_100000", "2026-09-10_100000"


def run(coro):
    return asyncio.run(coro)


async def settle(pilot, n=40):
    for _ in range(n):
        await pilot.pause(0.08)
        if not pilot.app.workers._workers:
            break
    await pilot.pause(0.2)


@pytest.fixture
def world(tmp_path):
    w = type("W", (), {})()
    w.tmp = tmp_path
    w.home1 = tmp_path / "snap1" / "home"
    (w.home1 / "Documents").mkdir(parents=True)
    (w.home1 / "Documents" / "a.txt").write_text("from snapshot a")
    (w.home1 / "Documents" / "b.txt").write_text("from snapshot b")
    (w.home1 / "notes.md").write_text("notes")
    w.home2 = tmp_path / "snap2" / "home"
    w.root2 = tmp_path / "snap2" / "root"
    w.home2.mkdir(parents=True)
    (w.home2 / "mine.txt").write_text("m")
    (w.root2 / "etc").mkdir(parents=True)
    (w.root2 / "etc" / "hosts").write_text("127.0.0.1 localhost")
    w.target = tmp_path / "live_home"
    w.target.mkdir()
    w.s1 = home_snapshot(K1, w.home1, target=w.target)
    w.s2 = two_area_snapshot(K2, w.home2, w.root2, w.target)
    return w


def app_for(world, services=None):
    from restore_fusion.ui.restore_app import RestoreApp

    svc = services or FakeServices(local=[world.s1, world.s2])
    eng = RestoreEngine(user="x", home=world.target, target_uid=0, target_gid=0)
    return RestoreApp(svc, eng, user_home=world.target), svc


def file_names(app):
    return [e["name"] for e in app.entries]


async def open_snapshot(pilot, app, key):
    app.select_snapshot(key)
    await settle(pilot)


def test_timeline_is_merged_and_nothing_is_selected(world):
    async def go():
        usb_and_box = remote_snapshot(K1, label="Box", target=world.target)
        svc = FakeServices(
            local=[world.s1, world.s2],
            remote=[usb_and_box, remote_snapshot(K3, target=world.target)],
        )
        app, _ = app_for(world, svc)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            tags = {s.key: s.tag for s in app.snapshots}
            assert tags == {K3: "Box", K2: "USB", K1: "USB+Box"}
            assert [s.key for s in app.snapshots] == [K3, K2, K1]
            assert app.selected == set() and app.active is None
            await open_snapshot(pilot, app, K1)
            assert app.selected == set(), "choosing a snapshot must not select anything"
            assert "Documents" in file_names(app)

    run(go())


def test_enter_on_the_timeline_opens_the_snapshot(world):
    from textual.widgets import ListView

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            lv = app.query_one("#snapshot_list", ListView)
            lv.focus()
            lv.index = [s.key for s in app.snapshots].index(K1)
            await pilot.press("enter")
            await settle(pilot)
            assert app.active.key == K1 and "notes.md" in file_names(app)

    run(go())


def test_space_selects_a_toggles_and_snapshot_switch_clears(world):
    from textual.widgets import ListView

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            fl = app.query_one("#file_list", ListView)
            fl.focus()
            fl.index = 1 + file_names(app).index("notes.md")  # +1: el ".." inicial
            await pilot.press("space")
            assert app.selected == {"notes.md"}
            await pilot.press("space")
            assert app.selected == set()
            await pilot.press("a")
            assert app.selected == set(file_names(app))
            await pilot.press("a")
            assert app.selected == set()
            await pilot.press("a")
            await open_snapshot(pilot, app, K2)  # otro snapshot: se vacía
            assert app.selected == set()

    run(go())


def test_restore_with_nothing_selected_opens_no_dialog(world):
    from textual.screen import ModalScreen
    from restore_fusion.ui.modals import ConfirmRestoreModal

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            await pilot.press("r")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmRestoreModal)
            await pilot.press("e")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ModalScreen)

    run(go())


def select(app, *names):
    app.selected = set(names)
    app._render_entries("/")


def test_confirm_has_no_default_button_and_enter_cancels(world):
    from textual.widgets import Button
    from restore_fusion.ui.modals import ConfirmRestoreModal

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("r")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmRestoreModal)
            assert app.focused.id == "btn-cancel", "initial focus must be Cancel"
            assert all(
                b.variant == "default" for b in app.screen.query(Button)
            ), "no button is preferred"
            await pilot.press("enter")
            await settle(pilot)
            assert not isinstance(app.screen, ConfirmRestoreModal)
        assert not (world.target / "notes.md").exists(), "Enter must not restore anything"

    run(go())


def test_backup_choice_keeps_the_existing_file_and_an_older_bak(world):
    async def go():
        (world.target / "notes.md").write_text("LIVE EDIT")
        (world.target / "notes.md.bak").write_text("OLDER BACKUP")
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("r")
            await pilot.pause(0.3)
            await pilot.press("b")
            await settle(pilot)
            assert app.screen.final_text == "Operation completed successfully"
        assert (world.target / "notes.md").read_text() == "notes"
        assert (world.target / "notes.md.bak").read_text() == "OLDER BACKUP"
        assert (world.target / "notes.md.bak.1").read_text() == "LIVE EDIT"

    run(go())


@pytest.mark.parametrize("key,expect", [("o", "notes"), ("s", "LIVE EDIT")])
def test_overwrite_and_skip(world, key, expect):
    async def go():
        (world.target / "notes.md").write_text("LIVE EDIT")
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("r")
            await pilot.pause(0.3)
            await pilot.press(key)
            await settle(pilot)
        assert (world.target / "notes.md").read_text() == expect

    run(go())


def test_cancel_key_in_the_dialog_restores_nothing(world):
    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("r")
            await pilot.pause(0.3)
            await pilot.press("escape")
            await settle(pilot)
        assert not (world.target / "notes.md").exists()

    run(go())


def test_a_failing_file_ends_as_restored_with_errors(world):
    async def go():
        real = shutil.copy2

        def flaky(src, dst, *a, **k):
            if Path(src).name == "b.txt":
                raise PermissionError(13, "Permission denied")
            return real(src, dst, *a, **k)

        app, _ = app_for(world)
        with (
            mock.patch("restore_fusion.restore.engine.shutil.copy2", side_effect=flaky),
            mock.patch("restore_fusion.restore.engine.shutil.which", return_value=None),
        ):
            async with app.run_test(size=(140, 40)) as pilot:
                await settle(pilot)
                await open_snapshot(pilot, app, K1)
                app.load_dir("Documents")
                select(app, "Documents/a.txt", "Documents/b.txt")
                app.rel = "Documents"
                await pilot.press("r")
                await pilot.pause(0.3)
                await pilot.press("o")
                await settle(pilot)
                text = app.screen.final_text
                assert text.startswith("Restored with errors") and "1 failed" in text
        assert (world.target / "Documents" / "a.txt").exists()
        assert not (world.target / "Documents" / "b.txt").exists()

    run(go())


def test_extract_to_a_custom_folder(world):
    from textual.widgets import Input

    async def go():
        app, _ = app_for(world)
        out = world.tmp / "custom_out"
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("e")
            await pilot.pause(0.3)
            app.screen.query_one(Input).value = str(out)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("o")
            await settle(pilot)
        assert (out / "notes.md").read_text() == "notes"
        assert not (world.target / "notes.md").exists()

    run(go())


def test_extract_refuses_slash_and_empty(world):
    from textual.widgets import Input

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("e")
            await pilot.pause(0.3)
            inp = app.screen.query_one(Input)
            for bad in ("/", "   "):
                inp.value = bad
                await pilot.press("enter")
                await pilot.pause(0.2)
                assert type(app.screen).__name__ == "CustomPathModal"
                assert inp.value != bad
        assert not (world.target / "notes.md").exists()

    run(go())


def test_multiple_areas_are_listed_and_system_restore_is_flagged(world):
    from restore_fusion.ui.modals import ConfirmRestoreModal

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K2)
            assert [e["name"] for e in app.entries] == ["Home", "Root /"] and app.area is None
            app.open_area(app.origin.area("root"))
            await settle(pilot)
            assert "etc" in file_names(app)
            app.load_dir("etc")
            select(app, "etc/hosts")
            await pilot.press("r")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmRestoreModal)
            assert len(app.screen.query("#system-warning")) == 1
            assert app.screen.target_path == Path("/")
            await pilot.press("escape")
            await pilot.pause(0.2)
            # ".." en la raíz del área vuelve a la lista de áreas
            app.load_dir("")
            from textual.widgets import ListView

            fl = app.query_one("#file_list", ListView)
            fl.focus()
            fl.index = 0
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert [e["name"] for e in app.entries] == ["Home", "Root /"] and app.selected == set()

    run(go())


def test_home_restore_is_not_flagged_as_system(world):
    from restore_fusion.ui.modals import ConfirmRestoreModal

    async def go():
        app, _ = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K1)
            select(app, "notes.md")
            await pilot.press("r")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmRestoreModal)
            assert len(app.screen.query("#system-warning")) == 0
            await pilot.press("escape")

    run(go())


def test_remote_only_snapshot_is_prepared_before_browsing(world):
    async def go():
        remote_root = world.tmp / "mounted"
        (remote_root / "Docs").mkdir(parents=True)
        (remote_root / "Docs" / "r.txt").write_text("remote")
        svc = FakeServices(
            local=[world.s1],
            remote=[remote_snapshot(K3, target=world.target)],
            prepare=lambda o, a, p: remote_root,
        )
        app, _ = app_for(world, svc)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await open_snapshot(pilot, app, K3)
            assert svc.prepared == [("Box", "home")]
            assert "Docs" in file_names(app)
            select(app, "Docs")
            await pilot.press("r")
            await pilot.pause(0.3)
            await pilot.press("b")
            await settle(pilot)
        assert (world.target / "Docs" / "r.txt").read_text() == "remote"

    run(go())


def test_prepare_failure_is_shown_not_swallowed(world):
    async def go():
        def boom(o, a, p):
            raise RuntimeError("No btrfs filesystem found for restore staging")

        svc = FakeServices(
            local=[world.s1], remote=[remote_snapshot(K3, target=world.target)], prepare=boom
        )
        app, _ = app_for(world, svc)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            app.select_snapshot(K3)
            await settle(pilot)
            assert app.screen.final_text == "Error Encountered" and app.area_root is None

    run(go())


def test_remote_notes_are_shown_as_warnings(world):
    async def go():
        svc = FakeServices(
            local=[world.s1], notes=["Box not responding (check network / SSH / config)"]
        )
        app, _ = app_for(world, svc)
        seen = []
        orig = app.notify
        app.notify = lambda msg, **k: (seen.append(msg), orig(msg, **k))[1]
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
        assert any("not responding" in m for m in seen)

    run(go())


def test_quit_asks_first_and_cleans_up(world):
    from restore_fusion.ui.modals import ConfirmExitModal

    async def go():
        app, svc = app_for(world)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            await pilot.press("q")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmExitModal)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmExitModal)
            await pilot.press("q")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(0.3)
        assert svc.cleaned >= 1

    run(go())


def test_empty_timeline_says_so(world):
    from textual.widgets import Label

    async def go():
        app, _ = app_for(world, FakeServices())
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            assert "No usable snapshots" in str(app.query_one("#status_line", Label).render())

    run(go())
