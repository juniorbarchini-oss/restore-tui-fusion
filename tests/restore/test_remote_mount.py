"""Montajes sshfs de solo lectura. Portado de btrfs-restore-tui (test_remote_mount.py)."""

import os
import subprocess
from pathlib import Path

import pytest

from restore_fusion.restore.remote_mount import RemoteMounter, sshfs_argv


def cp(rc=0, err=""):
    return subprocess.CompletedProcess([], rc, "", err)


def mk(tmp_path, *, rc=0, mounted=True, which=lambda n: "/usr/bin/sshfs"):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return cp(rc if argv[0] == "sshfs" else 0)

    m = RemoteMounter(tmp_path / "mnt", run=run, which=which, is_mount=lambda p: mounted)
    m.calls = calls
    return m


def do_mount(m, **kw):
    return m.mount(
        user="bob",
        host="h.example",
        remote_path=kw.pop("remote_path", "/backups/home/home_x"),
        slug=kw.pop("slug", "home_x"),
        **kw,
    )


def test_returns_none_without_sshfs(tmp_path):
    assert do_mount(mk(tmp_path, which=lambda n: None)) is None


def test_sshfs_argv_is_argv_only_and_read_only():
    argv = sshfs_argv("bob@h.example", "/backups/home/home_x", Path("/mnt/x"), 2222)
    assert argv[:3] == ["sshfs", "bob@h.example:/backups/home/home_x", "/mnt/x"]
    assert "ro" in argv and "reconnect" in argv and "2222" in argv
    assert "StrictHostKeyChecking=accept-new" in argv and not any(
        "=no" in a and "Strict" in a for a in argv
    )


def test_hostile_remote_path_stays_one_argument():
    evil = '/b/x"; rm -rf / #'
    argv = sshfs_argv("bob@h", evil, Path("/mnt/x"))
    assert sum("rm -rf" in a for a in argv) == 1 and f"bob@h:{evil}" in argv


def test_identity_and_known_hosts_when_present(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("k")
    (tmp_path / ".ssh" / "known_hosts").write_text("h")
    argv = sshfs_argv("u@h", "/p", Path("/m"), home=tmp_path)
    assert f"IdentityFile={tmp_path / '.ssh' / 'id_ed25519'}" in argv
    assert f"UserKnownHostsFile={tmp_path / '.ssh' / 'known_hosts'}" in argv


def test_failed_mount_leaves_no_mountpoint_and_no_tracked_mount(tmp_path):
    m = mk(tmp_path, rc=1, mounted=False)
    assert do_mount(m) is None
    assert m.mounts == [] and not (m.base.exists() and any(m.base.iterdir()))


def test_mount_that_returns_ok_but_is_not_a_mountpoint_is_a_failure(tmp_path):
    m = mk(tmp_path, rc=0, mounted=False)
    assert do_mount(m) is None and m.mounts == []


def test_successful_mount_is_tracked_and_cleanup_unmounts_it(tmp_path):
    m = mk(tmp_path)
    mnt = do_mount(m)
    assert mnt is not None and m.mounts == [mnt] and mnt.name.startswith(f"{os.getpid()}-")
    m.cleanup()
    assert any(c[0] in ("fusermount3", "fusermount", "umount") and str(mnt) in c for c in m.calls)
    assert m.mounts == [] and not mnt.exists()


def test_cleanup_uses_rmdir_never_rmtree(tmp_path):
    """Si el desmontaje fallara, un rmtree recorrería el contenido del montaje remoto."""
    m = mk(tmp_path)
    mnt = do_mount(m)
    (mnt / "precious").write_text("x")  # simula un montaje que NO se soltó
    m.cleanup()
    assert (mnt / "precious").read_text() == "x"


def test_sweep_removes_dead_run_mounts_keeps_live_ones_and_foreign_dirs(tmp_path):
    m = mk(tmp_path)
    m.base.mkdir(parents=True)
    live = m.base / f"{os.getpid()}-home_x"
    dead = m.base / "999999999-home_y"
    foreign = m.base / "someone-elses-stuff"
    for d in (live, dead, foreign):
        d.mkdir()
    m.sweep_orphans()
    assert live.exists() and not dead.exists() and foreign.exists()
    assert any("-uz" in c for c in m.calls)  # desmontaje perezoso del muerto


def test_mount_sweeps_orphans_first(tmp_path):
    m = mk(tmp_path)
    m.base.mkdir(parents=True)
    dead = m.base / "999999999-old"
    dead.mkdir()
    do_mount(m)
    assert not dead.exists()
