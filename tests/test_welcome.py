"""Pantalla de bienvenida: texto exacto del diseño (4.1), se muestra una vez, se puede saltar y
nunca bloquea el uso no interactivo. El menú es rich (no Textual), así que no lleva needs_textual.
"""

import io
from unittest import mock

from rich.console import Console

from restore_fusion import cli, welcome
from restore_fusion import maintenance
from restore_fusion.config.schema import Config


def capture():
    buf = io.StringIO()
    return Console(file=buf, width=120), buf


def test_text_has_every_section_of_the_design():
    t = welcome.WELCOME_TEXT
    for needle in (
        "Welcome to restore-fusion",
        "BACKUP FORMATS",
        "btrfs snapshots",
        "rsync copy",
        "DESTINATIONS",
        "USB / local disk",
        "  - Another computer        over SSH\n"
        "      btrfs source: the server must be btrfs, or hold a btrfs image\n"
        "      ext4 source:  no special requirement\n",
        "Proxmox Backup Server   optional",
        "NOT SUPPORTED",
        "exFAT, NTFS, FAT disks",
        "Network shares (SMB, NFS)",
    ):
        assert needle in t
    n = welcome.NETWORK_SHARE_NOTICE
    assert "Network shares (SMB/NFS) are not used directly" in n
    assert "use an SSH" in n and "Settings > Remote" in n and "btrfs image" in n


def test_show_prints_text_and_notice_and_remembers(tmp_path):
    out, buf = capture()
    assert welcome.show_welcome(out, lambda: "", home=tmp_path) is True
    assert "BACKUP FORMATS" in buf.getvalue() and "Settings > Remote" in buf.getvalue()
    assert welcome.was_shown(tmp_path)


def test_first_run_shows_once(tmp_path):
    out, buf = capture()
    assert welcome.first_run(out, tmp_path, interactive=True, read=lambda: "") is True
    buf2 = io.StringIO()
    assert (
        welcome.first_run(Console(file=buf2), tmp_path, interactive=True, read=lambda: "") is False
    )
    assert buf2.getvalue() == ""


def test_non_interactive_never_shows_or_writes(tmp_path):
    out, buf = capture()
    assert welcome.first_run(out, tmp_path, interactive=False) is False
    assert buf.getvalue() == "" and not welcome.state_file(tmp_path).exists()


def test_eof_skips_without_marking_as_seen(tmp_path):
    def eof():
        raise EOFError

    out, _ = capture()
    assert welcome.show_welcome(out, eof, home=tmp_path) is False
    assert not welcome.was_shown(tmp_path)


def test_unwritable_state_never_breaks(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    welcome.mark_shown(blocker)  # el padre es un archivo: debe tragarse el error
    out, _ = capture()
    assert welcome.show_welcome(out, lambda: "", home=blocker) is True


def test_menu_key_w_shows_welcome_and_returns_to_menu(monkeypatch):
    seq = iter(["w", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli.welcome, "show_welcome") as sw,
        mock.patch.object(cli, "_print_menu") as pm,
    ):
        assert cli.main([]) == 0
    sw.assert_called_once()
    assert pm.call_count == 2


def test_menu_lists_w_and_it_collides_with_nothing():
    assert "w" not in cli.MODES
    assert not set(cli.MODES) & {"b", "f", "q", "w"}
    assert set(cli.MODES) == {"r", "s", "p", "x"}


def test_backup_subcommand_does_not_touch_the_welcome(monkeypatch):
    with (
        mock.patch.object(cli, "_run_backup", return_value=0),
        mock.patch.object(cli.welcome, "first_run") as fr,
    ):
        assert cli.main(["backup-now", "--dry-run"]) == 0
    fr.assert_not_called()


def test_paths_lists_the_state_file(tmp_path):
    cfg = Config(user="t", home=tmp_path / "home", local_snapshots_dir=tmp_path / "s")
    rows = dict(maintenance.paths_report(cfg, tmp_path / "home"))
    assert str(welcome.state_file(tmp_path / "home")) in rows
