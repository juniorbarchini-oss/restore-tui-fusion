"""Pantalla Settings con Textual Pilot. Portado de btrfs-restore-tui/tests/test_settings_ui.py
(checkbox SSH, guardar sin host, guardar unidad+remoto, cancelar) más casos nuevos: ENGINE, pin
desconectado, autodetección, validación, errores del guardado, modo 600 y placeholders."""

import asyncio
import re
import stat
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Input, RadioSet

from restore_fusion.config import save_user_settings
from restore_fusion.config.schema import Config
from restore_fusion.ui.settings import SettingsApp

pytestmark = pytest.mark.needs_textual


def cfg(**kw):
    return Config(user="tester", home=Path("/nonexistent-home"), **kw)


class Rec:
    """Saver falso: guarda lo recibido sin tocar disco."""

    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, updates):
        if self.exc:
            raise self.exc
        self.calls.append(updates)
        return Path("/fake/config.conf")


def run(coro):
    return asyncio.run(coro)


def drive(path):
    return SettingsApp._drive_id(Path(path))


def test_ssh_checkbox_toggles_input_disabled_state():
    async def go():
        app = SettingsApp(cfg=cfg(), drives=[], saver=Rec())
        async with app.run_test(size=(100, 60)) as pilot:
            host = app.query_one("#input-host", Input)
            assert host.disabled
            app.query_one("#remote-enabled", Checkbox).value = True
            await pilot.pause()
            assert not host.disabled

    run(go())


def test_save_without_host_while_enabled_shows_error_and_writes_nothing():
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            app.query_one("#remote-enabled", Checkbox).value = True
            await pilot.pause()
            app._save()
            await pilot.pause()
            assert "host" in str(app.query_one("#settings-status").render()).lower()
        assert rec.calls == [] and not app.saved

    run(go())


def test_save_without_path_while_enabled_is_refused():
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            app.query_one("#remote-enabled", Checkbox).value = True
            app.query_one("#input-host", Input).value = "h.example"
            await pilot.pause()
            app._save()
            await pilot.pause()
            assert "path" in str(app.query_one("#settings-status").render()).lower()
        assert rec.calls == []

    run(go())


def test_save_writes_selected_drive_engine_and_remote(tmp_path):
    usb = tmp_path / "USB1"
    usb.mkdir()

    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[(usb, "ext4")], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.click(f"#{drive(usb)}")
            await pilot.click("#engine-rsync")
            app.query_one("#remote-enabled", Checkbox).value = True
            app.query_one("#input-host", Input).value = "h.example"
            app.query_one("#input-path", Input).value = "/srv/b"
            await pilot.pause()
            app._save()
            await pilot.pause()
        return rec, app

    rec, app = run(go())
    (u,) = rec.calls
    assert u["TARGET_DIR"] == str(usb)
    assert u["ENGINE"] == "rsync"
    assert u["REMOTE_ENABLED"] == "true" and u["REMOTE_HOST"] == "h.example"
    assert u["REMOTE_PATH"] == "/srv/b"
    assert app.saved and app.saved_path == Path("/fake/config.conf")


def test_cancel_writes_nothing():
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            app.action_cancel()
            await pilot.pause()
        return rec, app

    rec, app = run(go())
    assert rec.calls == [] and not app.saved


def test_nothing_is_preselected_when_nothing_is_configured(tmp_path):
    """Sin TARGET_DIR el único marcado es 'auto-detect' (no una unidad concreta)."""
    usb = tmp_path / "USB1"
    usb.mkdir()

    async def go():
        app = SettingsApp(cfg=cfg(), drives=[(usb, "ext4")], saver=Rec())
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.pause()
            return app.query_one("#target-radio", RadioSet).pressed_button.id

    assert run(go()) == "target-auto"


def test_engine_radio_shows_current_value():
    async def go():
        app = SettingsApp(cfg=cfg(engine="btrfs"), drives=[], saver=Rec())
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.pause()
            return app._engine_choice()

    assert run(go()) == "btrfs"


def test_unplugged_pin_is_kept_not_overwritten(tmp_path):
    pin = tmp_path / "gone"

    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(target_dir=pin), drives=[], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.pause()
            assert app.query_one("#target-keep")
            app._save()
            await pilot.pause()
        return rec

    (u,) = run(go()).calls
    assert u["TARGET_DIR"] == str(pin)


def test_auto_detect_writes_empty_target_dir(tmp_path):
    usb = tmp_path / "USB1"
    usb.mkdir()

    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(target_dir=usb), drives=[(usb, "ext4")], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.click("#target-auto")
            app._save()
            await pilot.pause()
        return rec

    (u,) = run(go()).calls
    assert u["TARGET_DIR"] == ""


@pytest.mark.parametrize("field,value", [("port", "abc"), ("port", "0"), ("max-gb", "-3")])
def test_invalid_numbers_are_rejected(field, value):
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[], saver=rec)
        async with app.run_test(size=(100, 60)) as pilot:
            app.query_one("#remote-enabled", Checkbox).value = True
            app.query_one("#input-host", Input).value = "h.example"
            app.query_one("#input-path", Input).value = "/p"
            app.query_one(f"#input-{field}", Input).value = value
            await pilot.pause()
            app._save()
            await pilot.pause()
        return rec

    assert run(go()).calls == []


def test_saver_error_is_shown_not_raised():
    async def go():
        app = SettingsApp(cfg=cfg(), drives=[], saver=Rec(exc=ValueError("boom")))
        async with app.run_test(size=(100, 60)) as pilot:
            app._save()
            await pilot.pause()
            return str(app.query_one("#settings-status").render()), app.saved

    msg, saved = run(go())
    assert "boom" in msg and not saved


def test_real_saver_writes_mode_600_and_roundtrips(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    def saver(updates):
        return save_user_settings(updates, user="tester", home=home, etc_dir=tmp_path / "etc")

    async def go():
        app = SettingsApp(cfg=cfg(), drives=[], saver=saver)
        async with app.run_test(size=(100, 60)) as pilot:
            await pilot.click("#engine-btrfs")
            app._save()
            await pilot.pause()
        return app

    app = run(go())
    assert app.saved
    assert stat.S_IMODE(app.saved_path.stat().st_mode) == 0o600
    assert "ENGINE=btrfs" in app.saved_path.read_text()


def test_placeholders_contain_no_real_ips():
    import restore_fusion.ui.settings as mod

    src = Path(mod.__file__).read_text()
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", src)


# -- PBS (opcional): campo enmascarado, modo 600 y no reescribe claves ajenas -------------------------
def _pbs_ids():
    return ("#input-pbs-repository", "#input-pbs-password", "#input-pbs-fingerprint")


def test_pbs_section_and_masked_password_field():
    async def go():
        app = SettingsApp(cfg=cfg(pbs_password="old-secret"), drives=[], saver=Rec())
        async with app.run_test(size=(100, 80)) as pilot:
            pw = app.query_one("#input-pbs-password", Input)
            assert pw.password is True
            assert pw.value == "" and "old-secret" not in str(pw.placeholder)
            texts = " ".join(str(w.render()) for w in app.query(".section"))
            assert "PROXMOX BACKUP SERVER (optional)" in texts

    run(go())


def test_pbs_untouched_fields_are_not_sent():
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(pbs_repository="u@pbs!t@h.example:d", pbs_password="x"),
                          drives=[], saver=rec)  # fmt: skip
        async with app.run_test(size=(100, 80)) as pilot:
            app.query_one("#remote-enabled", Checkbox).value = False
            await pilot.pause()
            app.action_save()
            await pilot.pause()
        (updates,) = rec.calls
        assert not any(k.startswith("PBS_") for k in updates)

    run(go())


def test_pbs_new_values_are_sent_and_secret_never_shown_in_status(tmp_path):
    async def go():
        rec = Rec()
        app = SettingsApp(cfg=cfg(), drives=[], saver=rec)
        async with app.run_test(size=(100, 80)) as pilot:
            app.query_one("#input-pbs-repository", Input).value = "u@pbs!t@h.example:d"
            app.query_one("#input-pbs-password", Input).value = "n3w-secret"
            app.action_save()
            await pilot.pause()
            assert "n3w-secret" not in str(app.query_one("#settings-status").render())
        (updates,) = rec.calls
        assert updates["PBS_PASSWORD"] == "n3w-secret"
        assert updates["PBS_REPOSITORY"] == "u@pbs!t@h.example:d"
        assert "PBS_FINGERPRINT" not in updates

    run(go())


def test_save_user_settings_keeps_600_and_other_keys_with_pbs(tmp_path):
    conf = tmp_path / ".config" / "restore-tui" / "config.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("EXCLUDE=Downloads/\nPBS_FINGERPRINT=aa:bb\nPBS_PASSWORD=old\n")
    save_user_settings({"PBS_PASSWORD": "new"}, user="tester", home=tmp_path)
    text = conf.read_text()
    assert "PBS_PASSWORD=new" in text and "PBS_FINGERPRINT=aa:bb" in text
    assert "EXCLUDE=Downloads/" in text
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600


def test_destination_text_does_not_read_like_a_requirement():
    async def go():
        app = SettingsApp(cfg=cfg(), drives=[(Path("/media/u/x"), "ext4")], saver=Rec())
        async with app.run_test(size=(100, 80)) as pilot:
            shown = " ".join(str(w.render()) for w in app.query("Label"))
            radio = str(app.query_one("#target-auto").label)
        assert "Pick a drive from the list. Auto-detect only chooses a drive that already" in shown
        assert "backup or has the label below." in shown
        assert radio == "Auto-detect (a drive that already holds a backup, or has the label below)"
        assert "labelled" not in shown + radio

    run(go())
