"""RestoreServices: qué hay para restaurar (USB de ambos motores, local, remoto) y cómo se prepara."""

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.config.schema import Config
from restore_fusion.model.snapshots import Area, Origin, Storage
from restore_fusion.restore.remote_mount import RemoteMounter
from restore_fusion.restore.services import RestoreServices, usb_mount
from restore_fusion.restore import sources


def seed_tree(mount: Path, dirname: str, name: str, layout: str):
    d = mount / dirname / "snapshots" / name
    d.mkdir(parents=True)
    ts = name.replace("-", "").replace("_", "")[:14]
    compact = f"{ts[:8]}_{ts[8:14]}"
    if layout == "rsync":
        (d / "home").mkdir()
        (d / "manifest.json").write_text('{"status": "completed", "engine": "rsync"}')
    else:
        (d / f"home_{compact}" / "tester").mkdir(parents=True)
        (d / f"root_{compact}").mkdir()
        (d / "manifest.json").write_text('{"status": "completed", "engine": "btrfs"}')


@pytest.fixture
def env(tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    cfg = Config(
        user="tester",
        home=tmp_path / "home",
        target_dir=str(usb),
        local_snapshots_dir=tmp_path / ".snapshots",
    )
    return cfg, usb


class NoMount(RemoteMounter):
    def mount(self, **kw):
        self.asked = kw
        return None


def svc(cfg, **kw):
    kw.setdefault("is_mount", lambda p: True)
    kw.setdefault("mounter", NoMount())
    return RestoreServices(cfg, **kw)


def test_reads_both_engine_folders_of_a_shared_usb(env):
    cfg, usb = env
    seed_tree(usb, "restore-tui", "2026-09-08_100000", "rsync")
    seed_tree(usb, "btrfs-restore", "2026-09-09_100000", "btrfs")
    snaps = svc(cfg).local_snapshots()
    assert [s.key for s in snaps] == ["2026-09-09_100000", "2026-09-08_100000"]
    assert {s.key: s.origins[0].engine for s in snaps} == {
        "2026-09-08_100000": "rsync",
        "2026-09-09_100000": "btrfs",
    }


def test_unmounted_usb_is_not_read_and_says_why(env):
    cfg, usb = env
    seed_tree(usb, "restore-tui", "2026-09-08_100000", "rsync")
    s = svc(cfg, is_mount=lambda p: False)
    assert s.local_snapshots() == [] and "not mounted" in s.notes[0]


def test_usb_mount_refuses_a_stale_directory(env):
    cfg, usb = env
    assert usb_mount(cfg, is_mount=lambda p: False)[0] is None
    assert usb_mount(cfg, is_mount=lambda p: True)[0] == usb


def test_autodetect_never_falls_back_to_a_random_drive(tmp_path):
    media = tmp_path / "media"
    (media / "RANDOM").mkdir(parents=True)
    cfg = Config(user="u", home=tmp_path)
    mount, why = usb_mount(cfg, is_mount=lambda p: True, bases=[media])
    assert mount is None and "no backup drive" in why


def test_local_snapshots_dir_is_merged_with_usb(env):
    cfg, usb = env
    seed_tree(usb, "btrfs-restore", "2026-09-06_153000", "btrfs")
    (cfg.local_snapshots_dir / "home_20260906_153000" / "tester").mkdir(parents=True)
    [s] = svc(cfg).local_snapshots()
    assert s.key == "2026-09-06_153000" and s.tag == "USB+Local"
    assert s.best_origin().label == "Local"  # lo local se lee primero


def test_nothing_anywhere_is_an_empty_list_not_a_crash(env):
    cfg, _ = env
    assert svc(cfg).local_snapshots() == []


# ---- remoto -----------------------------------------------------------------------------------


def test_remote_not_configured(env):
    cfg, _ = env
    assert svc(cfg).remote_snapshots() == ([], [])


def test_remote_rsync_tree_is_read_through_the_readonly_mount(env, tmp_path):
    cfg, _ = env
    cfg.remote_host, cfg.remote_path, cfg.remote_name = "h.example", "/r", "Box"
    mnt = tmp_path / "mnt"
    seed_tree(mnt.parent, "mnt", "2026-09-10_100000", "rsync")  # <mnt>/snapshots/<ts>/home

    class OkMount(RemoteMounter):
        def mount(self, **kw):
            self.asked = kw
            return mnt

    m = OkMount()
    snaps, notes = svc(
        cfg, mounter=m, scan_remote=lambda **k: ([], sources.RemoteStatus.UNREACHABLE, 0)
    ).remote_snapshots()
    assert [s.key for s in snaps] == ["2026-09-10_100000"] and snaps[0].origins[0].label == "Box"
    assert m.asked["remote_path"] == "/r/restore-tui" and notes == []  # el árbol sí respondió


def test_remote_btrfs_listing_and_status_notes(env):
    cfg, _ = env
    cfg.remote_host, cfg.remote_path, cfg.remote_name = "h.example", "/srv/b", "Box"
    for status, text in (
        (sources.RemoteStatus.UNREACHABLE, "not responding"),
        (sources.RemoteStatus.NO_PRIVILEGE, "sudo btrfs"),
    ):
        seen = {}

        def fake(**k):
            seen.update(k)
            return [], status, 0

        _snaps, notes = svc(cfg, scan_remote=fake).remote_snapshots()
        assert any(text in n for n in notes) and seen["base"] == "/srv/b"


def test_remote_uses_the_lan_host_first(env):
    cfg, _ = env
    cfg.remote_host, cfg.remote_host_lan, cfg.remote_path = "wan.example", "lan.example", "/r"
    seen = {}
    svc(
        cfg, scan_remote=lambda **k: seen.update(k) or ([], sources.RemoteStatus.CONNECTED, 0)
    ).remote_snapshots()
    assert seen["host"] == "lan.example"


# ---- prepare ----------------------------------------------------------------------------------------------


def test_prepare_tree_area_is_returned_as_is(env, tmp_path):
    cfg, _ = env
    a = Area("home", "Home", tmp_path / "x", Path("/home/t"))
    assert svc(cfg).prepare(Origin("USB", "usb"), a) == tmp_path / "x"


def test_prepare_remote_area_uses_sshfs_and_descends_into_the_user_dir(env, tmp_path):
    cfg, _ = env
    cfg.remote_host, cfg.remote_path = "h.example", "/r"
    mnt = tmp_path / "mnt"
    (mnt / "tester").mkdir(parents=True)

    class OkMount(RemoteMounter):
        def mount(self, **kw):
            self.asked = kw
            return mnt

    s = svc(cfg, mounter=OkMount())
    a = Area("home", "Home", "/r/home/home_x", Path("/home/t"), Storage.REMOTE, True)
    assert s.prepare(Origin("Box", "remote"), a) == mnt / "tester"
    a2 = Area("root", "Root /", "/r/root/root_x", Path("/"), Storage.REMOTE)
    assert s.prepare(Origin("Box", "remote"), a2) == mnt


def test_prepare_falls_back_to_staging_when_the_mount_fails(env):
    cfg, _ = env
    staging = mock.Mock()
    staging.deploy.return_value = Path("/stg/x")
    s = svc(cfg, staging=staging)  # NoMount => None
    a = Area("home", "Home", "/r/home/home_x", Path("/home/t"), Storage.REMOTE, True)
    got = s.prepare(Origin("Box", "remote"), a, progress=lambda t: None)
    assert got == Path("/stg/x") and staging.deploy.call_args.args[0] is a


def test_prepare_stream_goes_straight_to_staging(env, tmp_path):
    cfg, _ = env
    staging = mock.Mock()
    staging.deploy.return_value = Path("/stg/y")
    a = Area("root", "Root /", tmp_path / "root.btrfs.zst", Path("/"), Storage.STREAM)
    assert svc(cfg, staging=staging).prepare(Origin("USB", "usb"), a) == Path("/stg/y")


def test_cleanup_cleans_staging_and_mounts_and_never_raises(env):
    cfg, _ = env
    staging, mounter = mock.Mock(), mock.Mock()
    staging.cleanup_own.side_effect = RuntimeError("x")
    s = svc(cfg, staging=staging, mounter=mounter)
    s.cleanup()
    mounter.cleanup.assert_called_once()
