"""restore-now: elevación solo con btrfs, error de motor, y registro en el menú."""

from unittest import mock

from restore_fusion import cli, cli_backup, cli_restore
from restore_fusion.config.schema import Config


def test_restore_is_registered_in_the_menu():
    assert cli.MODES["r"] == "restore_fusion.cli_restore"
    with mock.patch.object(cli, "_run_mode", return_value=0) as rm:
        cli.main(["restore"])
    rm.assert_called_once_with("restore_fusion.cli_restore")


def test_elevation_names_the_restore_module():
    calls = []
    assert cli_backup.maybe_elevate(
        "btrfs",
        ["x"],
        module="restore_fusion.cli_restore",
        geteuid=lambda: 1000,
        execvp=lambda f, a: calls.append(a),
        environ={},
    )
    assert calls[0][-3:] == ["-m", "restore_fusion.cli_restore", "x"] or calls[0][-2:] == [
        "restore_fusion.cli_restore",
        "x",
    ]


def test_main_exits_2_on_an_invalid_engine(monkeypatch):
    monkeypatch.setattr(cli_restore.cfgmod, "load", lambda: Config(user="u", engine="zfs"))
    assert cli_restore.main([]) == 2


def test_main_runs_the_app_without_sudo_on_rsync(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_restore.cfgmod, "load", lambda: Config(user="u", home=tmp_path))
    monkeypatch.setattr(cli_restore, "source_is_btrfs", lambda *a, **k: False)
    ran = []
    import restore_fusion.ui.restore_app as ra

    monkeypatch.setattr(ra.RestoreApp, "run", lambda self, *a, **k: ran.append(self))
    monkeypatch.setattr(cli_restore, "maybe_elevate", lambda *a, **k: False)
    assert cli_restore.main([]) == 0 and len(ran) == 1
    ran[0].services.cleanup()


def test_main_returns_after_a_sudo_re_exec(monkeypatch):
    monkeypatch.setattr(cli_restore.cfgmod, "load", lambda: Config(user="u"))
    monkeypatch.setattr(cli_restore, "source_is_btrfs", lambda *a, **k: True)
    monkeypatch.setattr(cli_restore, "btrfs_tool_present", lambda: True)
    monkeypatch.setattr(cli_restore, "maybe_elevate", lambda *a, **k: True)
    assert cli_restore.main(["--x"]) == 0
