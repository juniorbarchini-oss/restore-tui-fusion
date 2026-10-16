"""PBS push: qué sube cada motor, argv exactos, secreto solo por entorno. Cliente simulado.
Portado de btrfs test_cli_pbs.py (latest_snapshot) + argv/exclusiones/guardas nuevos."""

import io
import subprocess
from pathlib import Path

import pytest
from pbs_fakes import SECRET
from rich.console import Console

from restore_fusion.pbs import push
from restore_fusion.pbs.client import PbsError
from restore_fusion.pbs.push import (
    power_saver_available as real_available,
)  # el autouse parchea push.*


def out():
    buf = io.StringIO()
    return Console(file=buf, width=200, force_terminal=False), buf


def make_snaps(d, names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).mkdir()


class Runner:
    def __init__(self, rc=0, exc=None):
        self.calls, self.rc, self.exc = [], rc, exc

    def __call__(self, argv, env=None):
        self.calls.append((argv, env))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc)


def go(cfg, engine, runner, *, euid=0, which=lambda t: "/usr/bin/" + t):
    console, buf = out()
    rc = push.run_push(
        cfg, engine, geteuid=lambda: euid, runner=runner, which=which, hostname="box", out=console
    )
    return rc, buf.getvalue()


# -- latest_snapshot (portado) ----------------------------------------------------------------------
def test_latest_picks_newest_by_name(tmp_path):
    make_snaps(tmp_path, ["root_20260910_150549", "root_20260913_234101", "root_20260909_230829"])
    assert push.latest_snapshot(tmp_path, "root").name == "root_20260913_234101"


def test_latest_ignores_other_kind_and_files(tmp_path):
    make_snaps(tmp_path, ["home_20260913_234101", "root_20260912_000000"])
    (tmp_path / "root_20260913_999999").touch()
    assert push.latest_snapshot(tmp_path, "root").name == "root_20260912_000000"


def test_latest_ignores_symlinks(tmp_path):
    make_snaps(tmp_path, ["root_20260910_000000"])
    (tmp_path / "root_20260999_999999").symlink_to(tmp_path / "root_20260910_000000")
    assert push.latest_snapshot(tmp_path, "root").name == "root_20260910_000000"


def test_latest_none_cases(tmp_path):
    assert push.latest_snapshot(tmp_path, "root") is None
    assert push.latest_snapshot(tmp_path / "nope", "root") is None


# -- btrfs -------------------------------------------------------------------------------------------------
def test_btrfs_pushes_newest_ro_snapshots_without_exclusions(cfg):
    make_snaps(cfg.local_snapshots_dir, ["root_1", "root_2", "home_1", "home_3"])
    r = Runner()
    rc, text = go(cfg, "btrfs", r)
    assert rc == 0 and "complete" in text
    ((argv, env),) = r.calls
    snaps = cfg.local_snapshots_dir
    assert argv == [
        "proxmox-backup-client", "backup", f"root.pxar:{snaps}/root_2", f"home.pxar:{snaps}/home_3",
        "--repository", cfg.pbs_repository, "--backup-id", "box",
    ]  # fmt: skip
    assert "--exclude" not in argv
    assert "live copy" not in text.lower()


def test_btrfs_needs_root_and_calls_nothing_otherwise(cfg):
    make_snaps(cfg.local_snapshots_dir, ["root_1"])
    r = Runner()
    rc, text = go(cfg, "btrfs", r, euid=1000)
    assert rc == 1 and "root" in text and r.calls == []


def test_btrfs_without_snapshots_asks_for_a_backup_first(cfg):
    r = Runner()
    rc, text = go(cfg, "btrfs", r)
    assert rc == 1 and "Backup now" in text and r.calls == []


def test_btrfs_pushes_only_what_exists(cfg):
    make_snaps(cfg.local_snapshots_dir, ["home_1"])
    r = Runner()
    go(cfg, "btrfs", r)
    assert [a for a in r.calls[0][0] if a.endswith("home_1") or a.startswith("root.pxar")] == [
        f"home.pxar:{cfg.local_snapshots_dir}/home_1"
    ]


# -- rsync (ext4) -----------------------------------------------------------------------------------------------
def test_rsync_pushes_live_home_with_exclusions_and_warns(cfg):
    r = Runner()
    rc, text = go(cfg, "rsync", r, euid=1000)  # el motor rsync no necesita root
    assert rc == 0
    ((argv, env),) = r.calls
    assert argv[:3] == ["proxmox-backup-client", "backup", f"home.pxar:{cfg.home}"]
    assert "root.pxar" not in " ".join(argv)
    ex = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    assert "Downloads/" in ex and len(ex) > 1  # extra del usuario + las integradas
    assert "not a snapshot" in text


def test_rsync_missing_home_is_an_error(cfg):
    cfg.home.rmdir()
    rc, text = go(cfg, "rsync", Runner(), euid=1000)
    assert rc == 1 and "not found" in text


# -- guardas y secreto ---------------------------------------------------------------------------------------------
def test_not_configured_prints_help_without_calling(cfg):
    cfg.pbs_password = ""
    r = Runner()
    rc, text = go(cfg, "rsync", r, euid=1000)
    assert rc == 1 and "PBS_REPOSITORY" in text and r.calls == []


def test_tool_missing(cfg):
    rc, text = go(cfg, "rsync", Runner(), euid=1000, which=lambda t: None)
    assert rc == 1 and "not found" in text


def test_secret_only_in_env_never_in_argv_or_output(cfg):
    make_snaps(cfg.local_snapshots_dir, ["root_1", "home_1"])
    r = Runner()
    rc, text = go(cfg, "btrfs", r)
    argv, env = r.calls[0]
    assert SECRET not in " ".join(argv) and SECRET not in text
    assert env["PBS_PASSWORD"] == SECRET and env["PBS_FINGERPRINT"] == "AA:BB"
    assert SECRET not in repr(cfg)


def test_no_fingerprint_means_no_env_key(cfg, monkeypatch):
    monkeypatch.delenv("PBS_FINGERPRINT", raising=False)
    cfg.pbs_fingerprint = ""
    r = Runner()
    go(cfg, "rsync", r, euid=1000)
    assert "PBS_FINGERPRINT" not in r.calls[0][1]


def test_exit_code_is_propagated_and_reported(cfg):
    rc, text = go(cfg, "rsync", Runner(rc=3), euid=1000)
    assert rc == 3 and "exited with code 3" in text and "complete" not in text


def test_ctrl_c_returns_130(cfg):
    rc, _ = go(cfg, "rsync", Runner(exc=KeyboardInterrupt()), euid=1000)
    assert rc == 130


def test_pbs_freeze_raises_when_nothing_to_push(cfg):
    with pytest.raises(PbsError):
        push.pbs_freeze("btrfs", cfg)


def test_tool_missing_message_says_optional_and_names_no_package(cfg):
    rc, text = go(cfg, "rsync", Runner(), euid=1000, which=lambda t: None)
    assert "optional" in text and "Proxmox repository" in text
    for pkg in ("apt install", "pacman -S", "yay", "sudo apt"):
        assert pkg not in text


# -- power-saver mientras corre el push ---------------------------------------------------------
class ListRunner:
    """Doble de `powerprofilesctl list`."""

    def __init__(self, stdout="", rc=0, exc=None):
        self.stdout, self.rc, self.exc, self.calls = stdout, rc, exc, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, stdout=self.stdout)


def test_power_saver_wraps_the_backup_when_available(cfg, monkeypatch):
    monkeypatch.setattr(push, "power_saver_available", lambda *a, **k: True)
    r = Runner()
    rc, text = go(cfg, "rsync", r, euid=1000)
    argv = r.calls[0][0]
    assert rc == 0
    assert argv[:5] == ["powerprofilesctl", "launch", "--profile", "power-saver", "--appid"]
    sep = argv.index("--")
    assert argv[sep + 1] == push.TOOL and argv[sep + 2] == "backup"
    assert "power-saver while this runs" in text


def test_power_saver_keeps_secret_out_of_argv_and_env_intact(cfg, monkeypatch):
    monkeypatch.setattr(push, "power_saver_available", lambda *a, **k: True)
    r = Runner()
    go(cfg, "rsync", r, euid=1000)
    argv, env = r.calls[0]
    assert SECRET not in " ".join(argv)
    assert env["PBS_PASSWORD"] == SECRET


def test_power_saver_not_used_when_unavailable(cfg):
    r = Runner()
    go(cfg, "rsync", r, euid=1000)
    assert r.calls[0][0][0] == push.TOOL


def test_power_saver_can_be_turned_off_in_config(cfg, monkeypatch):
    monkeypatch.setattr(push, "power_saver_available", lambda *a, **k: True)
    cfg.pbs_power_saver = False
    r = Runner()
    go(cfg, "rsync", r, euid=1000)
    assert r.calls[0][0][0] == push.TOOL


def test_power_saver_exit_code_still_propagates(cfg, monkeypatch):
    monkeypatch.setattr(push, "power_saver_available", lambda *a, **k: True)
    rc, text = go(cfg, "rsync", Runner(rc=3), euid=1000)
    assert rc == 3 and "exited with code 3" in text


def test_available_false_without_the_tool():
    assert real_available(lambda t: None, ListRunner()) is False


def test_available_true_when_profile_listed():
    out_ = "  performance:\n\n* balanced:\n\n  power-saver:\n"
    r = ListRunner(stdout=out_)
    assert real_available(lambda t: "/usr/bin/" + t, r) is True
    assert r.calls == [["powerprofilesctl", "list"]]


def test_available_false_when_profile_not_offered():
    r = ListRunner(stdout="* balanced:\n  performance:\n")
    assert real_available(lambda t: "/usr/bin/" + t, r) is False


@pytest.mark.parametrize("exc", [OSError("boom"), subprocess.TimeoutExpired("powerprofilesctl", 5)])
def test_available_false_when_the_probe_fails(exc):
    r = ListRunner(exc=exc)
    assert real_available(lambda t: "/usr/bin/" + t, r) is False


def test_available_false_when_list_exits_nonzero():
    r = ListRunner(stdout="power-saver", rc=1)
    assert real_available(lambda t: "/usr/bin/" + t, r) is False
