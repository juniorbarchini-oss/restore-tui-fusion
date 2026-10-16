"""LocalDestination: punto de montaje real, latest solo en completed, borrar solo con Approval."""

import os
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.destinations import LocalDestination
from restore_fusion.safety.approval import ApprovalError

SNAP = "2026-01-01_100000"


def mk(tmp_path, mounted=True, **kw):
    mount = tmp_path / "usb"
    mount.mkdir(exist_ok=True)
    state = {"mounted": mounted}
    dest = LocalDestination(mount / "restore-tui", is_mount=lambda p: state["mounted"], **kw)
    return dest, state


def test_stale_mountpoint_dir_is_not_reachable(tmp_path):
    dest, _ = mk(tmp_path, mounted=False)
    ok, why = dest.reachable()
    assert not ok and "not a mount point" in why


def test_stale_mountpoint_nothing_is_written(tmp_path):
    dest, _ = mk(tmp_path, mounted=False)
    assert dest.ensure_ready()[0] is False
    with pytest.raises(OSError):
        dest.reserve_snapshot(SNAP)
    assert not (tmp_path / "usb" / "restore-tui").exists()


def test_missing_mount_dir_is_not_reachable(tmp_path):
    dest = LocalDestination(tmp_path / "nope" / "restore-tui", is_mount=lambda p: True)
    assert dest.reachable()[0] is False


def test_unplugged_between_check_and_write_is_caught(tmp_path):
    dest, state = mk(tmp_path)
    assert dest.reachable()[0]
    state["mounted"] = False  # se desenchufa tras el chequeo inicial
    with pytest.raises(OSError):
        dest.reserve_snapshot(SNAP)


def test_ready_and_reserve_with_collisions(tmp_path):
    dest, _ = mk(tmp_path)
    assert dest.ensure_ready() == (True, "writable")
    assert dest.reserve_snapshot(SNAP) == SNAP
    assert dest.reserve_snapshot(SNAP) == SNAP + "_2"
    assert dest.reserve_snapshot(SNAP) == SNAP + "_3"


def test_list_ignores_symlinks_files_and_bad_names(tmp_path):
    dest, _ = mk(tmp_path)
    dest.ensure_ready()
    (dest.snaps / SNAP).mkdir()
    (dest.snaps / "junk").mkdir()
    (dest.snaps / "2026-01-02_100000").symlink_to(dest.snaps / SNAP)
    (dest.snaps / "2026-01-03_100000").write_text("file")
    assert dest.list_snapshots() == [SNAP]


def test_manifest_roundtrip_and_garbage(tmp_path):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    assert dest.read_manifest(SNAP) == {}
    dest.write_manifest(SNAP, {"status": "running"})
    assert dest.read_manifest(SNAP) == {"status": "running"}
    (dest.snaps / SNAP / "manifest.json").write_text("[1]")
    assert dest.read_manifest(SNAP) == {}


@pytest.mark.parametrize("status", ["running", "partial", "failed", None])
def test_latest_refuses_non_completed(tmp_path, status):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    if status:
        dest.write_manifest(SNAP, {"status": status})
    with pytest.raises(ValueError):
        dest.update_latest(SNAP)
    assert not dest.latest.exists() and not dest.latest.is_symlink()


def test_latest_moves_on_completed_and_keeps_old_target_on_refusal(tmp_path):
    dest, _ = mk(tmp_path)
    a = dest.reserve_snapshot(SNAP)
    dest.write_manifest(a, {"status": "completed"})
    dest.update_latest(a)
    assert dest.latest.is_symlink() and os.readlink(dest.latest) == f"snapshots/{a}"
    b = dest.reserve_snapshot("2026-01-02_100000")
    dest.write_manifest(b, {"status": "partial"})
    with pytest.raises(ValueError):
        dest.update_latest(b)
    assert dest.latest.resolve() == (dest.snaps / a).resolve()  # sigue donde estaba
    c = dest.reserve_snapshot("2026-01-03_100000")
    dest.write_manifest(c, {"status": "completed"})
    dest.update_latest(c)
    assert dest.latest.resolve() == (dest.snaps / c).resolve()
    assert not (dest.root / ".latest.tmp").exists()


def test_latest_rejects_traversal_names(tmp_path):
    dest, _ = mk(tmp_path)
    dest.ensure_ready()
    with pytest.raises(ValueError):
        dest.update_latest("../../etc")


# ---- borrar: solo con Approval ---------------------------------------------------


def test_remove_without_approval_is_refused(tmp_path):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    with pytest.raises(ApprovalError):
        dest.remove(SNAP, None)
    assert (dest.snaps / SNAP).is_dir()


def test_remove_with_approval_for_another_snapshot_is_refused(tmp_path, approve):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    with pytest.raises(ApprovalError):
        dest.remove(SNAP, approve("2026-02-02_100000"))
    assert (dest.snaps / SNAP).is_dir()


@pytest.mark.parametrize("bad", ["..", "../x", "latest", "", "2026-01-01"])
def test_remove_rejects_non_snapshot_names(tmp_path, approve, bad):
    dest, _ = mk(tmp_path)
    dest.ensure_ready()
    with pytest.raises(ValueError):
        dest.remove(bad, approve(bad))


def test_remove_with_approval_deletes(tmp_path, approve):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    dest.remove(SNAP, approve(SNAP))
    assert not (dest.snaps / SNAP).exists()


# ---- el resto --------------------------------------------------------------------


def test_push_meta_copies_and_marks_scripts_executable(tmp_path):
    dest, _ = mk(tmp_path)
    dest.reserve_snapshot(SNAP)
    stage = tmp_path / "stage"
    (stage / "_system_state").mkdir(parents=True)
    (stage / "_system_state" / "os.json").write_text("{}")
    (stage / "restore.sh").write_text("#!/bin/sh\n")
    dest.push_meta(stage, SNAP)
    assert (dest.snaps / SNAP / "_system_state" / "os.json").exists()
    assert os.access(dest.snaps / SNAP / "restore.sh", os.X_OK)


def test_write_kit_files(tmp_path):
    dest, _ = mk(tmp_path)
    dest.ensure_ready()
    dest.write_kit_files({"disaster-recovery.sh": b"#!/bin/sh\n", "RECOVERY.md": b"x"})
    assert os.access(dest.root / "disaster-recovery.sh", os.X_OK)
    assert (dest.root / "RECOVERY.md").read_bytes() == b"x"


def test_should_prune_more_respects_floor(tmp_path):
    dest, _ = mk(tmp_path, max_disk_percent=80)
    dest.ensure_ready()
    usage = mock.Mock(used=90, total=100)
    with mock.patch("restore_fusion.destinations.local.shutil.disk_usage", return_value=usage):
        assert dest.should_prune_more(5, 2) is True
        assert dest.should_prune_more(2, 2) is False  # piso MIN_KEEP
        assert dest.should_prune_more(1, 0) is False  # nunca el último
        assert "90%" in dest.usage_summary()
    usage = mock.Mock(used=10, total=100)
    with mock.patch("restore_fusion.destinations.local.shutil.disk_usage", return_value=usage):
        assert dest.should_prune_more(9, 2) is False


def test_usage_unknown_never_prunes(tmp_path):
    dest, _ = mk(tmp_path)
    with mock.patch("restore_fusion.destinations.local.shutil.disk_usage", side_effect=OSError):
        assert dest.should_prune_more(9, 2) is False


def test_run_cmd_is_argv(tmp_path):
    dest, _ = mk(tmp_path)
    marker = tmp_path / "pwned"
    res = dest.run_cmd(["echo", f"; touch {marker}"])
    assert res.stdout.strip() == f"; touch {marker}" and not marker.exists()
