"""backup-now: confirmaciones (--yes no aprueba borrados), elevación, flujo principal."""

import io
import sys
from pathlib import Path
from unittest import mock

import pytest

from btrfs_fakes import FakeBtrfsOps
from stubs import StubPM
from restore_fusion import cli_backup
from restore_fusion.backup.targets import Candidates, candidate_destinations
from restore_fusion.config.schema import Config
from restore_fusion.destinations import LocalDestination, RemoteQueryError, SSHDestination
from restore_fusion.engines import RsyncHardlinkEngine
from restore_fusion.engines.base import FullCheck
from restore_fusion.safety import Approval

TTY = {"is_tty": lambda: True}


class Eng:
    """Motor mínimo para probar las confirmaciones."""

    def __init__(self, check=FullCheck.NO, engine_id="rsync", partial=None, partial_error=None):
        self.id, self._check, self._partial, self._err = engine_id, check, partial, partial_error

    def would_be_full(self, dest):
        return self._check

    def partial_copies(self, dest):
        if self._err:
            raise self._err
        return self._partial


class D:
    label = "Remote"

    def display_root(self):
        return "u@h:/r"


@pytest.fixture
def silent(monkeypatch):
    monkeypatch.setattr(cli_backup.console, "print", lambda *a, **k: None)


def answers(*vals):
    it = iter(vals)

    def fn(_prompt=""):
        v = next(it)
        if isinstance(v, BaseException):
            raise v
        return v

    return fn


def tty(monkeypatch, ans):
    from restore_fusion.safety import confirm as _c  # función

    mod = sys.modules["restore_fusion.safety.confirm"]
    monkeypatch.setattr(mod, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", ans)


# ---- confirmación de backup completo (port de test_cli_confirm.py) --------------------------------


def test_incremental_asks_nothing(silent, monkeypatch):
    tty(monkeypatch, answers("n"))
    inp = mock.Mock()
    monkeypatch.setattr("builtins.input", inp)
    assert cli_backup.confirm_full(D(), Eng(FullCheck.NO), False)
    inp.assert_not_called()


def test_full_asks_and_yes_continues(silent, monkeypatch):
    tty(monkeypatch, answers("y"))
    assert cli_backup.confirm_full(D(), Eng(FullCheck.YES), False)


def test_full_default_is_no(silent, monkeypatch):
    tty(monkeypatch, answers(""))
    assert not cli_backup.confirm_full(D(), Eng(FullCheck.YES), False)


def test_full_without_a_terminal_is_no(silent):
    assert not cli_backup.confirm_full(D(), Eng(FullCheck.YES), False)  # pytest: sin tty


def test_yes_flag_skips_the_full_backup_prompt(silent, monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=AssertionError("prompted")))
    assert cli_backup.confirm_full(D(), Eng(FullCheck.YES), True)


def test_unknown_asks_and_default_is_no(silent, monkeypatch):
    tty(monkeypatch, answers(""))
    assert not cli_backup.confirm_full(D(), Eng(FullCheck.UNKNOWN), False)


def test_unknown_with_yes_is_skipped_never_a_blind_full_send(silent, monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=AssertionError("prompted")))
    assert not cli_backup.confirm_full(D(), Eng(FullCheck.UNKNOWN), True)


def test_unknown_prompt_says_could_not_check(monkeypatch):
    shown = []
    monkeypatch.setattr(
        cli_backup.console, "print", lambda *a, **k: shown.append(str(a[0].renderable))
    )
    tty(monkeypatch, answers("n"))
    cli_backup.confirm_full(D(), Eng(FullCheck.UNKNOWN), False)
    assert "could not check" in shown[0]


# ---- copias a medias: solo un 'y' explícito (port) ---------------------------------------------------------


P = [
    {"kind": "home", "name": "home_20260915_001833", "has_children": False},
    {"kind": "root", "name": "root_20260915_001833", "has_children": True},
]


def ssh_dest():
    return SSHDestination(host="h.example", root="/r", user="u", tcp_check=lambda h, p: True)


def test_nothing_partial_asks_nothing(silent, monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=AssertionError("prompted")))
    assert cli_backup.ask_heal(ssh_dest(), Eng(engine_id="btrfs", partial=[])) is None


def test_yes_approves_only_copies_without_dependents(silent, monkeypatch):
    tty(monkeypatch, answers("y"))
    ap = cli_backup.ask_heal(ssh_dest(), Eng(engine_id="btrfs", partial=P))
    assert isinstance(ap, Approval)
    assert ap.covers("delete", "home/home_20260915_001833")
    assert not ap.covers("delete", "root/root_20260915_001833")


def test_default_and_no_terminal_delete_nothing(silent, monkeypatch):
    tty(monkeypatch, answers(""))
    assert cli_backup.ask_heal(ssh_dest(), Eng(engine_id="btrfs", partial=P)) is None
    monkeypatch.setattr(
        sys.modules["restore_fusion.safety.confirm"], "_stdin_is_tty", lambda: False
    )
    assert cli_backup.ask_heal(ssh_dest(), Eng(engine_id="btrfs", partial=P)) is None


def test_eof_deletes_nothing(silent, monkeypatch):
    tty(monkeypatch, answers(EOFError()))
    assert cli_backup.ask_heal(ssh_dest(), Eng(engine_id="btrfs", partial=P)) is None


def test_query_error_is_silent_and_deletes_nothing(silent, monkeypatch):
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=AssertionError("prompted")))
    eng = Eng(engine_id="btrfs", partial_error=RemoteQueryError("x"))
    assert cli_backup.ask_heal(ssh_dest(), eng) is None


def test_only_btrfs_remote_destinations_have_partial_copies(silent):
    assert cli_backup.ask_heal(ssh_dest(), Eng(engine_id="rsync", partial=P)) is None
    assert cli_backup.ask_heal(D(), Eng(engine_id="btrfs", partial=P)) is None


def test_yes_cannot_approve_a_deletion_through_the_cli(silent, monkeypatch):
    # el parser tiene --yes, pero ask_heal no recibe ningún flag
    import inspect

    assert "yes" not in "".join(inspect.signature(cli_backup.ask_heal).parameters)
    assert cli_backup.build_parser().parse_args(["--yes"]).yes is True


# ---- elevación solo cuando el motor lo necesita --------------------------------------------------------------


def test_btrfs_engine_not_root_re_executes_under_sudo():
    calls = []
    out = cli_backup.maybe_elevate(
        "btrfs",
        ["--quiet"],
        geteuid=lambda: 1000,
        execvp=lambda f, a: calls.append((f, a)),
        environ={"RESTORE_TUI_TARGET_DIR": "/x", "HOME": "/h", "PYTHONPATH": "/p"},
    )
    assert out and calls[0][0] == "sudo"
    argv = calls[0][1]
    assert (
        argv[0] == "sudo" and "--preserve-env=" in argv[1] or argv[1].startswith("--preserve-env=")
    )
    assert "RESTORE_TUI_TARGET_DIR" in argv[1] and "PYTHONPATH" in argv[1] and "HOME" not in argv[1]
    assert argv[-3:] == ["-m", "restore_fusion.cli_backup", "--quiet"]


def test_no_sudo_for_rsync_or_when_already_root():
    called = []
    assert not cli_backup.maybe_elevate(
        "rsync", [], geteuid=lambda: 1000, execvp=lambda *a: called.append(a)
    )
    assert not cli_backup.maybe_elevate(
        "btrfs", [], geteuid=lambda: 0, execvp=lambda *a: called.append(a)
    )
    assert called == []


# ---- destinos candidatos: regla 1 ------------------------------------------------------------------------------------


def test_unmounted_usb_is_not_a_candidate_and_says_why(tmp_path):
    cfg = Config(user="u", home=tmp_path, target_dir=str(tmp_path))
    c = candidate_destinations(cfg, "rsync", ("usb",), is_mount=lambda p: False)
    assert c.dests == [] and "not a mount point" in c.notes[0]


def test_no_drive_found_never_falls_back_to_the_first_one(tmp_path):
    media = tmp_path / "media"
    (media / "RANDOM").mkdir(parents=True)
    cfg = Config(user="u", home=tmp_path)
    c = candidate_destinations(cfg, "rsync", ("usb",), bases=[media], is_mount=lambda p: True)
    assert c.dests == [] and c.notes


def test_remote_not_configured_has_a_note(tmp_path):
    c = candidate_destinations(Config(user="u", home=tmp_path), "rsync", ("remote",))
    assert c.dests == [] and "not configured" in c.notes[0]


def test_remote_candidate_uses_the_engine_specific_root(tmp_path):
    cfg = Config(
        user="u", home=tmp_path, remote_host="h.example", remote_path="/r/", remote_name="Box"
    )
    rs = candidate_destinations(cfg, "rsync", ("remote",)).dests[0]
    bt = candidate_destinations(cfg, "btrfs", ("remote",)).dests[0]
    assert rs.root == "/r/restore-tui" and bt.root == "/r" and rs.label == "Box"


# ---- main() ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "a.txt").write_text("hello")
    (tmp_path / "usb").mkdir()
    cfg = Config(user="tester", home=home, local_snapshots_dir=tmp_path / "snaps", min_keep=2)
    monkeypatch.setattr(cli_backup.cfgmod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(cli_backup, "package_manager_here", lambda: (None, "test"))
    alerts = []
    monkeypatch.setattr(cli_backup, "send_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(cli_backup, "clear_alert", lambda *a, **k: alerts.append("cleared"))
    monkeypatch.setattr(cli_backup, "source_is_btrfs", lambda *a, **k: False)
    mounted = {"v": True}

    def cands(cfg_, engine_id, want, **kw):
        usb = LocalDestination(
            tmp_path / "usb" / "restore-tui", is_mount=lambda p: mounted["v"], label="USB"
        )
        return Candidates([usb] if "usb" in want else [], [])

    monkeypatch.setattr(cli_backup, "candidate_destinations", cands)

    class R:
        pass

    r = R()
    r.cfg, r.alerts, r.mounted, r.tmp = cfg, alerts, mounted, tmp_path
    r.main = lambda argv: cli_backup.main(argv)
    return r


@pytest.mark.needs_rsync
def test_main_backs_up_with_rsync_and_clears_the_alert(run_main, capsys):
    rc = run_main.main(["--quiet", "--yes", "--no-menu"])
    assert rc == 0
    snaps = list((run_main.tmp / "usb" / "restore-tui" / "snapshots").iterdir())
    assert len(snaps) == 1 and (snaps[0] / "home" / "Documents" / "a.txt").read_text() == "hello"
    assert "cleared" in run_main.alerts


def test_main_unmounted_usb_exits_2_and_writes_nothing(run_main):
    run_main.mounted["v"] = False
    rc = run_main.main(["--quiet", "--yes", "--no-menu"])
    assert rc == 2 and not (run_main.tmp / "usb" / "restore-tui").exists()


def test_main_full_backup_without_yes_and_without_a_terminal_does_nothing(run_main):
    rc = run_main.main(["--quiet", "--no-menu"])
    assert rc == 3 and not (run_main.tmp / "usb" / "restore-tui").exists()
    assert run_main.alerts  # se avisa que hace falta una terminal


@pytest.mark.needs_rsync
def test_main_dry_run_writes_nothing(run_main):
    (run_main.tmp / "usb" / "restore-tui" / "snapshots").mkdir(parents=True)
    rc = run_main.main(["--dry-run", "--no-menu"])
    assert rc == 0 and list((run_main.tmp / "usb" / "restore-tui" / "snapshots").iterdir()) == []


def test_main_invalid_engine_setting_exits_2(run_main):
    run_main.cfg.engine = "zfs"
    assert run_main.main(["--quiet", "--yes"]) == 2


@pytest.mark.needs_rsync
def test_main_failed_backup_exits_1_and_alerts(run_main, monkeypatch):
    monkeypatch.setattr(RsyncHardlinkEngine, "_rsync", lambda *a, **k: 12)
    rc = run_main.main(["--quiet", "--yes", "--no-menu"])
    assert rc == 1 and any(isinstance(a, tuple) and "FAILED" in a[0] for a in run_main.alerts)


def test_main_keyboard_interrupt_exits_130(run_main, monkeypatch):
    monkeypatch.setattr(
        "restore_fusion.cli_backup.BackupRunner.run", mock.Mock(side_effect=KeyboardInterrupt)
    )
    monkeypatch.setattr(cli_backup, "confirm_full", lambda *a, **k: True)
    assert run_main.main(["--quiet", "--yes", "--no-menu"]) == 130
