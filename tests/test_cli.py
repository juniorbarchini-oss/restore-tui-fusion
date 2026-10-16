"""cli.py: despacho de subcomandos y menú. Portado de btrfs-restore-tui/tests/test_cli_menu.py."""

import io
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion import cli
from restore_fusion.config.schema import Config


def test_backup_subcommand_routes_to_backup():
    with mock.patch.object(cli, "_run_backup", return_value=7) as rb:
        assert cli.main(["backup", "--dry-run"]) == 7
    rb.assert_called_once_with(["--dry-run"])
    with mock.patch.object(cli, "_run_backup", return_value=0) as rb:
        cli.main(["backup-now"])
    rb.assert_called_once_with([])


def test_menu_q_exits_cleanly(monkeypatch):
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: "q")
    with mock.patch.object(cli, "_print_menu"):
        assert cli.main([]) == 0


def test_menu_b_runs_backup_then_returns_to_menu(monkeypatch):
    seq = iter(["b", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli, "_run_backup", return_value=0) as rb,
        mock.patch.object(cli, "_print_menu") as pm,
    ):
        assert cli.main([]) == 0
    rb.assert_called_once()
    assert pm.call_count == 2  # al entrar y al volver del backup


def test_menu_rejects_garbage_then_quits(monkeypatch):
    seq = iter(["zzz", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    out = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: out.append(str(a[0]) if a else ""))
    with mock.patch.object(cli, "_print_menu"):
        assert cli.main([]) == 0
    assert any("Pick one" in o and "B" in o and "Q" in o for o in out)


def test_menu_eof_exits(monkeypatch):
    monkeypatch.setattr(cli.console, "input", mock.Mock(side_effect=EOFError))
    with mock.patch.object(cli, "_print_menu"):
        assert cli.main([]) == 0


def test_full_recovery_info_does_not_leave_the_menu(monkeypatch):
    seq = iter(["f", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli, "_show_recovery_info") as info,
        mock.patch.object(cli, "_print_menu") as pm,
    ):
        cli.main([])
    info.assert_called_once()
    assert pm.call_count == 1


def test_registered_modes_appear_in_the_menu_and_run(monkeypatch):
    monkeypatch.setitem(cli.MODES, "r", "restore_fusion.fake_mode")
    seq = iter(["r", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli, "_run_mode", return_value=0) as rm,
        mock.patch.object(cli, "_print_menu"),
    ):
        cli.main([])
    rm.assert_called_once_with("restore_fusion.fake_mode")


def test_run_mode_builds_module_command_and_returns_code():
    with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=3)) as run:
        assert cli._run_mode("restore_fusion.cli_backup", ["--x"]) == 3
    cmd = run.call_args.args[0]
    assert cmd[1:] == ["-m", "restore_fusion.cli_backup", "--x"]
    assert str(cli._PKG_ROOT) in run.call_args.kwargs["env"]["PYTHONPATH"]


def test_run_mode_keyboard_interrupt_is_130():
    with mock.patch.object(cli.subprocess, "run", side_effect=KeyboardInterrupt):
        assert cli._run_mode("m") == 130


def test_menu_header_shows_engine_and_no_secret(capsys):
    cli._print_menu()
    out = capsys.readouterr().out
    assert "Engine:" in out and "[B]" in out and "[Q]" in out


def test_settings_mode_is_registered():
    from restore_fusion import cli

    assert cli.MODES["s"] == "restore_fusion.ui.settings"


def test_pbs_modes_are_registered():
    from restore_fusion import cli

    assert cli.MODES["p"] == "restore_fusion.pbs.push"
    assert cli.MODES["x"] == "restore_fusion.pbs.restore"


# -- PBS en el menú: [P]/[X] se ocultan solo si PBS no está configurado NI instalado -------------------
def _pbs_cfg(configured):
    return Config(
        user="t",
        home=Path("/nonexistent-home"),
        pbs_repository="u@pbs!tok@h.example:ds" if configured else "",
        pbs_password="s3cret" if configured else "",
    )


@pytest.mark.parametrize(
    "configured,installed,visible",
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_pbs_visibility_matrix(configured, installed, visible):
    which = (lambda t: "/usr/bin/" + t) if installed else (lambda t: None)
    assert cli._pbs_visible(_pbs_cfg(configured), which) is visible


def _menu_text(capsys, configured, installed):
    with (
        mock.patch.object(cli.cfgmod, "load", return_value=_pbs_cfg(configured)),
        mock.patch.object(cli.shutil, "which", return_value="/x" if installed else None),
    ):
        cli._print_menu()
    return capsys.readouterr().out


def test_menu_hides_pbs_keys_when_neither_configured_nor_installed(capsys):
    out = _menu_text(capsys, False, False)
    assert "[P]" not in out and "[X]" not in out and "[B]" in out and "[S]" in out


@pytest.mark.parametrize("configured,installed", [(True, False), (False, True), (True, True)])
def test_menu_lists_pbs_keys_otherwise(capsys, configured, installed):
    out = _menu_text(capsys, configured, installed)
    assert "[P]" in out and "[X]" in out
    assert "s3cret" not in out


def test_hidden_pbs_keys_still_run_and_hint_omits_them(monkeypatch, capsys):
    seq = iter(["p", "zzz", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli.cfgmod, "load", return_value=_pbs_cfg(False)),
        mock.patch.object(cli.shutil, "which", return_value=None),
        mock.patch.object(cli, "_run_mode", return_value=1) as rm,
        mock.patch.object(cli, "_print_menu"),
        mock.patch.object(cli.welcome, "first_run"),
    ):
        cli.main([])
    rm.assert_called_once_with("restore_fusion.pbs.push")  # sigue funcionando para scripts
    hint = capsys.readouterr().out
    assert "Pick one: B, R, S, D, F, W, Q" in " ".join(hint.split())


def test_menu_u_updates_and_leaves_when_installed(monkeypatch):
    seq = iter(["u"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli.update, "run_update", return_value=True) as ru,
        mock.patch.object(cli, "_print_menu"),
    ):
        assert cli.main([]) == 0
    ru.assert_called_once()


def test_menu_u_not_updated_returns_to_menu(monkeypatch):
    seq = iter(["u", "q"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(seq))
    with (
        mock.patch.object(cli.update, "run_update", return_value=False),
        mock.patch.object(cli, "_print_menu") as pm,
    ):
        cli.main([])
    assert pm.call_count == 2
