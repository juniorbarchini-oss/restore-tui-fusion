"""Staging btrfs. Portado de btrfs-restore-tui (test_engine_staging.py, test_remote_mount.py: pv)."""

import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.config.schema import Config
from restore_fusion.engines.btrfs.staging import Staging
from restore_fusion.model.snapshots import Area, Storage


def cfg(**kw):
    c = Config(
        user="bob",
        home=Path("/nonexistent"),
        remote_host="h.example",
        remote_path="/backups",
        remote_user="bob",
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def area(storage=Storage.REMOTE, path="/backups/home_x", **kw):
    return Area(
        "home",
        "Home",
        path if storage is Storage.REMOTE else Path(path),
        Path("/home/bob"),
        storage,
        **kw,
    )


def mk(tmp_path, **kw):
    s = Staging(
        kw.pop("cfg", cfg()),
        kw.pop("progress", None),
        fstype=kw.pop("fstype", lambda p: "btrfs"),
        which=kw.pop("which", lambda n: None),
    )
    s.dir = tmp_path / "staging"
    return s


# ---- etapas ----------------------------------------------------------------------------------------


def test_tree_area_needs_no_staging(tmp_path):
    assert mk(tmp_path).stages(area(Storage.TREE), Path("/tmp/r"), "bob") is None


def test_remote_subvolume_pipeline_is_argv_lists(tmp_path):
    stages = mk(tmp_path).stages(area(), Path("/tmp/stg"), "bob")
    assert len(stages) == 2 and all(isinstance(st, list) for st in stages)
    assert stages[0][0] == "ssh" and "StrictHostKeyChecking=accept-new" in stages[0]
    assert stages[0][-2] == "bob@h.example" and stages[0][-1] == "sudo btrfs send /backups/home_x"
    assert stages[1] == ["btrfs", "receive", "/tmp/stg"]


def test_stream_has_zstd_stage(tmp_path):
    stages = mk(tmp_path).stages(
        area(Storage.STREAM, "/usb/x/home.btrfs.zst"), Path("/tmp/stg"), "bob"
    )
    assert stages == [["zstd", "-dc", "/usb/x/home.btrfs.zst"], ["btrfs", "receive", "/tmp/stg"]]


def test_malicious_path_stays_one_quoted_remote_argument(tmp_path):
    evil = '/backups/x"; rm -rf / #'
    stages = mk(tmp_path).stages(area(path=evil), Path("/tmp/stg"), "bob")
    remote_cmd = stages[0][-1]
    assert remote_cmd.startswith("sudo btrfs send ") and remote_cmd.count("rm -rf") == 1
    import shlex

    assert shlex.split(remote_cmd)[-1] == evil  # llega como UN argumento al shell remoto


def test_port_option(tmp_path):
    stages = mk(tmp_path, cfg=cfg(remote_port=2222)).stages(area(), Path("/tmp/stg"), "bob")
    assert stages[0][stages[0].index("-p") + 1] == "2222"


def test_remote_not_configured_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="not configured"):
        mk(tmp_path, cfg=cfg(remote_host="")).stages(area(), Path("/tmp/stg"), "bob")


# ---- pv ----------------------------------------------------------------------------------------------


def test_no_pv_without_a_progress_callback(tmp_path):
    stages, idx = mk(tmp_path)._maybe_pv([["a"], ["b", "c"]])
    assert idx == -1 and stages == [["a"], ["b", "c"]]


def test_pv_spliced_before_the_final_stage(tmp_path):
    s = mk(tmp_path, progress=lambda t: None, which=lambda n: "/usr/bin/pv")
    stages, idx = s._maybe_pv([["ssh", "h", "x"], ["btrfs", "receive", "/d"]])
    assert stages[idx][0] == "pv" and stages[-1] == ["btrfs", "receive", "/d"] and len(stages) == 3


def test_no_pv_when_missing(tmp_path):
    s = mk(tmp_path, progress=lambda t: None, which=lambda n: None)
    assert s._maybe_pv([["a"], ["b"]])[1] == -1


@pytest.mark.skipif(not __import__("shutil").which("pv"), reason="pv not installed")
def test_progress_pipeline_reports_bytes(tmp_path):
    seen = []
    s = Staging(cfg(), seen.append)
    s.run_pipeline([["printf", "hello world"], ["cat"]], progress=True)
    assert any("B" in line for line in seen), seen


def test_pipeline_failure_reports_stderr(tmp_path):
    s = mk(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        s.run_pipeline([["sh", "-c", "echo boom >&2; exit 3"], ["cat"]])


def test_missing_command_is_a_runtime_error(tmp_path, monkeypatch):
    # PATH fijo: un directorio ilegible en el PATH del host daría PermissionError en vez de FileNotFoundError
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(RuntimeError, match="missing command"):
        mk(tmp_path).run_pipeline([["definitely-not-a-command-xyz"], ["cat"]])


def test_missing_parent_gets_a_plain_message(tmp_path):
    with pytest.raises(RuntimeError, match="parent subvolume missing"):
        mk(tmp_path).run_pipeline(
            [["sh", "-c", "echo 'cannot find parent subvolume' >&2; exit 1"], ["cat"]]
        )


def test_cancel_kills_a_stuck_transfer(tmp_path):
    s = mk(tmp_path)
    s._active = [subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(2)]
    procs = list(s._active)
    s.cancel()
    time.sleep(0.2)
    assert all(p.poll() is not None for p in procs) and s._active == []


# ---- carpeta de staging y limpieza -----------------------------------------------------------------------


def test_resolve_dir_prefers_configured_btrfs(tmp_path):
    s = mk(tmp_path, cfg=cfg(staging_dir=Path("/tmp/cfg-stg")))
    assert s.resolve_dir() == Path("/tmp/cfg-stg")


def test_resolve_dir_errors_when_nothing_is_btrfs(tmp_path):
    s = mk(tmp_path, fstype=lambda p: "ext4")
    with pytest.raises(RuntimeError, match="(?i)btrfs"):
        s.resolve_dir()


@pytest.fixture
def stg(tmp_path):
    s = mk(tmp_path)
    s.dir.mkdir()
    return s


def mkrun(s, name):
    d = s.dir / name
    d.mkdir()
    (d / "received").mkdir()
    (d / "received" / "f").write_text("x")
    return d


def no_btrfs():
    return mock.patch(
        "restore_fusion.engines.btrfs.staging.subprocess.run", side_effect=OSError("no btrfs here")
    )


def test_orphans_only_removes_dead_pids(stg):
    alive, dead = mkrun(stg, f"{os.getpid()}-home_x"), mkrun(stg, "999999999-home_y")
    with no_btrfs():
        stg.sweep_orphans()
    assert alive.exists() and not dead.exists()


def test_cleanup_all_removes_every_pid_named_run_but_nothing_else(stg):
    mkrun(stg, f"{os.getpid()}-a")
    mkrun(stg, "42-b")
    other = stg.dir / "my-own-folder"
    other.mkdir()
    (other / "important").write_text("keep")
    with no_btrfs():
        stg.cleanup_all()
    assert sorted(p.name for p in stg.dir.iterdir()) == ["my-own-folder"]
    assert (other / "important").read_text() == "keep"


def test_sweep_never_touches_non_pid_entries(stg):
    foreign = stg.dir / "snapshots-i-care-about"
    foreign.mkdir()
    with no_btrfs():
        stg.sweep_orphans()
    assert foreign.exists()


def test_cleanup_own_only_touches_the_tracked_run(stg):
    mine, other = mkrun(stg, f"{os.getpid()}-mine"), mkrun(stg, f"{os.getpid()}-other")
    stg._run_dir = mine
    with no_btrfs():
        stg.cleanup_own()
    assert not mine.exists() and other.exists() and stg._run_dir is None


def test_cleanup_own_refuses_a_run_dir_outside_the_staging_dir(stg, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stg._run_dir = outside
    with no_btrfs():
        stg.cleanup_own()
    assert outside.exists()


def test_deploy_tree_area_returns_it_untouched(stg, tmp_path):
    a = area(Storage.TREE, str(tmp_path / "x"))
    with mock.patch.object(stg, "resolve_dir", return_value=stg.dir):
        assert stg.deploy(a, "bob") == tmp_path / "x"
