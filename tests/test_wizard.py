"""Asistente de destinos (item E): nada preseleccionado, nada se escribe sin resumen confirmado,
no corre sin terminal interactivo. Todo con fakes: no toca discos ni SSH."""

import io
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from restore_fusion import cli, wizard
from restore_fusion.config.schema import Config

DRIVES = []


@pytest.fixture(autouse=True)
def drives_on_disk(tmp_path):
    """Directorios reales (validate_target exige que existan); is_mount se inyecta."""
    for n in ("EXT4", "FAT", "BTR"):
        (tmp_path / n).mkdir()
    DRIVES[:] = [
        (tmp_path / "EXT4", "ext4"),
        (tmp_path / "FAT", "exfat"),
        (tmp_path / "BTR", "btrfs"),
    ]


class Script:
    """Entrada simulada: devuelve las respuestas en orden y registra los prompts."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Saver:
    def __init__(self):
        self.calls = []

    def __call__(self, updates):
        self.calls.append(dict(updates))
        return Path("/fake/config.conf")


def go(*answers, engine="btrfs", drives=None, remote_fs=lambda *a: "btrfs", interactive=True):
    buf = io.StringIO()
    out = Console(file=buf, width=400)
    saver = Saver()
    cfg = Config(user="t", home=Path("/nonexistent-home"))
    res = wizard.run_wizard(
        out, cfg, engine=engine, read=Script(*answers), drives=DRIVES if drives is None else drives, remote_fs=remote_fs,
        saver=saver, is_mount=lambda p: True, interactive=interactive,
    )  # fmt: skip
    return res, saver, buf.getvalue()


def test_lists_drives_with_fs_and_flags_unusable_ones_and_preselects_nothing():
    res, saver, text = go("s")
    assert res is False and saver.calls == []
    assert (
        "1  " + str(DRIVES[0][0]) + "  (ext4)" in text
        and "3  " + str(DRIVES[2][0]) + "  (btrfs)" in text
    )
    assert "CANNOT BE USED" in text and "exfat" in text
    assert ".btrfs.zst stream files" in text  # ext4 con motor btrfs: aviso, no bloqueo
    assert "default" not in text.lower()


def test_empty_input_chooses_nothing_and_eof_writes_nothing():
    res, saver, text = go("", "")  # dos Enter vacíos y luego EOF
    assert res is False and saver.calls == [] and "Skipped" in text
    assert "Type a drive number" in text


def test_picking_a_drive_needs_confirmation_and_default_is_no():
    res, saver, _ = go("3", "")  # Enter vacío en el resumen = No
    assert res is False and saver.calls == []
    res, saver, _ = go("3", "n")
    assert res is False and saver.calls == []


def test_confirmed_drive_writes_only_target_dir():
    res, saver, text = go("3", "y")
    assert res is True and saver.calls == [{"TARGET_DIR": str(DRIVES[2][0])}]
    assert "nothing has been written yet" in text


def test_non_btrfs_drive_is_allowed_for_btrfs_engine_with_notice():
    res, saver, text = go("1", "y", engine="btrfs")
    assert res is True and saver.calls == [{"TARGET_DIR": str(DRIVES[0][0])}]


def test_unsupported_fs_cannot_be_picked_then_user_skips():
    res, saver, text = go("2", "s")
    assert res is False and saver.calls == []
    assert "cannot be used" in text


@pytest.mark.parametrize(
    "fs,usable", [("ext4", True), ("btrfs", True), ("xfs", False), (None, False)]
)
def test_rsync_engine_flags_anything_but_ext4_btrfs(fs, usable):
    assert wizard.drive_flag(fs, "rsync")[0] is usable


@pytest.mark.parametrize("fs", ["vfat", "exfat", "ntfs"])
def test_fat_family_is_unusable_with_any_engine(fs):
    assert wizard.drive_flag(fs, "btrfs")[0] is False
    assert wizard.drive_flag(fs, "rsync")[0] is False


def test_drive_that_is_no_longer_a_real_mount_is_refused_before_writing():
    buf = io.StringIO()
    saver = Saver()
    res = wizard.run_wizard(
        Console(file=buf, width=400), Config(user="t", home=Path("/x")), engine="btrfs",
        read=Script("3", "s"), drives=DRIVES, saver=saver, is_mount=lambda p: False,
        interactive=True,
    )  # fmt: skip
    assert res is False and saver.calls == []
    assert "not a mount point" in buf.getvalue()


def test_ssh_flow_runs_the_remote_check_and_writes_after_confirmation():
    seen = []

    def check(host, user, path, port):
        seen.append((host, user, path, port))
        return "btrfs"

    res, saver, text = go("a", "srv.example", "bob", "/srv/b", "2222", "y", remote_fs=check)
    assert res is True and seen == [("srv.example", "bob", "/srv/b", 2222)]
    assert saver.calls == [
        {"REMOTE_ENABLED": "true", "REMOTE_HOST": "srv.example", "REMOTE_USER": "bob",
         "REMOTE_PATH": "/srv/b", "REMOTE_PORT": "2222"}
    ]  # fmt: skip
    assert "Remote filesystem: btrfs" in text


def test_ssh_ext4_server_warns_for_btrfs_engine_but_can_still_be_saved():
    res, saver, text = go("a", "h", "u", "/p", "", "y", remote_fs=lambda *a: "ext2/ext3")
    assert res is True and saver.calls[0]["REMOTE_PORT"] == "22"
    assert "not btrfs" in text and "ENGINE=rsync" in text


def test_ssh_ext4_server_no_btrfs_warning_for_rsync_engine():
    _res, _saver, text = go(
        "a", "h", "u", "/p", "", "n", engine="rsync", remote_fs=lambda *a: "ext2/ext3"
    )
    assert "not btrfs" not in text and "Remote filesystem: ext2/ext3" in text


def test_ssh_check_failure_says_so_and_does_not_block():
    res, saver, text = go("a", "h", "u", "/p", "", "y", remote_fs=lambda *a: None)
    assert res is True and "Could not determine the remote filesystem" in text


@pytest.mark.parametrize(
    "answers", [("a", ""), ("a", "h", "", "/p", ""), ("a", "h", "u", "/p", "99999")]
)
def test_ssh_incomplete_or_bad_input_writes_nothing(answers):
    res, saver, _ = go(*answers)
    assert res is False and saver.calls == []


def test_ssh_declined_summary_writes_nothing():
    res, saver, _ = go("a", "h", "u", "/p", "", "")
    assert res is False and saver.calls == []


def test_non_interactive_never_prompts_or_writes():
    read = Script("1", "y")
    saver = Saver()
    res = wizard.run_wizard(
        Console(file=io.StringIO()), Config(user="t", home=Path("/x")), read=read,
        drives=DRIVES, saver=saver, interactive=False,
    )  # fmt: skip
    assert res is False and read.prompts == [] and saver.calls == []


# -- integración con el menú -----------------------------------------------------------------------------------
def test_menu_key_d_runs_the_wizard_and_is_listed(monkeypatch, capsys):
    seq = iter(["d", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli, "_destinations_wizard") as w,
        mock.patch.object(cli.welcome, "first_run", return_value=False),
        mock.patch.object(cli, "_print_menu"),
    ):
        assert cli.main([]) == 0
    w.assert_called_once()
    cli._print_menu()
    assert "[D]" in capsys.readouterr().out


def test_wizard_is_offered_right_after_the_first_welcome_only(monkeypatch):
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: "q")
    for shown, calls in ((True, 1), (False, 0)):
        with (
            mock.patch.object(cli, "_destinations_wizard") as w,
            mock.patch.object(cli.welcome, "first_run", return_value=shown),
            mock.patch.object(cli, "_print_menu"),
        ):
            cli.main([])
        assert w.call_count == calls


def test_wizard_crash_does_not_take_the_menu_down(monkeypatch):
    seq = iter(["d", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli, "_destinations_wizard", side_effect=RuntimeError("boom")),
        mock.patch.object(cli.welcome, "first_run", return_value=False),
        mock.patch.object(cli, "_print_menu"),
    ):
        assert cli.main([]) == 0


def test_backup_now_does_not_import_or_call_the_wizard():
    import restore_fusion.cli_backup as cb

    assert "wizard" not in open(cb.__file__).read()
