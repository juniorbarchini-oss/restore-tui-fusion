"""Escáneres de snapshots restaurables. Portado de btrfs-restore-tui (test_scanner.py,
test_scanner_target.py) + el modelo fusionado (orígenes y áreas)."""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.model.snapshots import (
    Area,
    Origin,
    Snapshot,
    Storage,
    key_from_compact,
    merge_timeline,
    parse_key_time,
)
from restore_fusion.restore import sources
from restore_fusion.restore.sources import RemoteStatus, scan_local, scan_remote_btrfs, scan_tree

HOME = Path("/home/tester")


def mk_snap(
    snaps: Path,
    name: str,
    status,
    *,
    layout="btrfs",
    streams=False,
    user="tester",
    engine_field=None,
    created=None,
):
    d = snaps / name
    d.mkdir(parents=True)
    if status is not None:
        m = {
            "snapshot": name,
            "status": status,
            "created_at": created or f"{name[:10]}T{name[11:13]}:{name[13:15]}:00",
        }
        if engine_field:
            m["engine"] = engine_field
        d_m = d / "manifest.json"
        d_m.write_text(json.dumps(m))
    ts = name.replace("-", "").replace("_", "")[:14]
    compact = f"{ts[:8]}_{ts[8:14]}"
    if layout == "rsync":
        (d / "home" / "Documents").mkdir(parents=True)
        if status is not None:
            (d / "manifest.json").write_text(
                json.dumps({**json.loads((d / "manifest.json").read_text()), "rsync_exit": 0})
            )
    elif streams:
        (d / "root.btrfs.zst").write_bytes(b"x")
        (d / "home.btrfs.zst").write_bytes(b"x")
    else:
        (d / f"root_{compact}").mkdir()
        (d / f"home_{compact}" / user).mkdir(parents=True)
    return d


@pytest.fixture
def snaps(tmp_path):
    s = tmp_path / "snapshots"
    s.mkdir()
    return s


def scan(snaps, **kw):
    kw.setdefault("label", "USB")
    kw.setdefault("kind", "usb")
    return scan_tree(snaps, user="tester", home=HOME, **kw)


# ---- árbol de snapshots ------------------------------------------------------------------------


def test_btrfs_completed_snapshot_yields_home_and_root_areas(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed")
    [s] = scan(snaps)
    [o] = s.origins
    assert s.status == "completed" and o.engine == "btrfs" and o.label == "USB"
    home, root = o.area("home"), o.area("root")
    assert home.path == snaps / "2026-09-08_100000" / "home_20260908_100000" / "tester"
    assert home.target_base == HOME and home.home_user_subdir and not home.is_system
    assert root.target_base == Path("/") and root.is_system and root.storage is Storage.TREE


def test_rsync_snapshot_has_one_home_area(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed", layout="rsync")
    [s] = scan(snaps)
    [a] = s.origins[0].areas
    assert s.origins[0].engine == "rsync" and a.kind == "home" and a.path.name == "home"
    assert a.target_base == HOME


def test_stream_snapshot_needs_staging(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed", streams=True)
    [s] = scan(snaps)
    assert {a.kind: a.storage for a in s.origins[0].areas} == {
        "home": Storage.STREAM,
        "root": Storage.STREAM,
    }


def test_home_subvolume_without_user_dir_is_the_whole_home(snaps):
    d = mk_snap(snaps, "2026-09-08_100000", "completed", user="someone-else")
    [s] = scan(snaps)
    a = s.origins[0].area("home")
    assert (
        a.path == d / "home_20260908_100000"
        and a.target_base == Path("/home")
        and not a.home_user_subdir
    )


def test_partial_is_listed_and_flagged_running_and_failed_are_skipped(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed")
    mk_snap(snaps, "2026-09-08_110000", "partial")
    mk_snap(snaps, "2026-09-08_120000", "running")
    mk_snap(snaps, "2026-09-08_130000", "failed")
    got = {s.key: s.status for s in scan(snaps)}
    assert got == {"2026-09-08_100000": "completed", "2026-09-08_110000": "partial"}


def test_missing_manifest_is_unknown_not_a_crash(snaps):
    mk_snap(snaps, "2026-09-08_100000", None)
    [s] = scan(snaps)
    assert s.status == "unknown" and s.usable


def test_garbage_manifest_is_unknown(snaps):
    d = mk_snap(snaps, "2026-09-08_100000", "completed")
    (d / "manifest.json").write_text("not json")
    assert scan(snaps)[0].status == "unknown"


def test_symlinks_files_and_bad_names_are_ignored(snaps):
    d = mk_snap(snaps, "2026-09-08_100000", "completed")
    (snaps / "latest").symlink_to(d)
    (snaps / "junk").mkdir()
    (snaps / "2026-09-09_100000").write_text("file")
    (snaps / "2026-09-10_100000_x").mkdir()
    assert [s.key for s in scan(snaps)] == ["2026-09-08_100000"]


def test_snapshot_without_any_area_is_skipped(snaps):
    d = snaps / "2026-09-08_100000"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"status": "completed"}))
    assert scan(snaps) == []


def test_timestamp_from_manifest_then_name(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed", created="2026-09-08T10:00:07")
    d = mk_snap(snaps, "2026-09-09_100000", "completed")
    m = json.loads((d / "manifest.json").read_text())
    m.pop("created_at")
    (d / "manifest.json").write_text(json.dumps(m))
    got = {s.key: s.timestamp for s in scan(snaps)}
    assert got["2026-09-08_100000"] == datetime(2026, 9, 8, 10, 0, 7)
    assert got["2026-09-09_100000"] == datetime(2026, 9, 9, 10, 0, 0)


def test_missing_dir_is_empty(tmp_path):
    assert scan(tmp_path / "nope") == []


def test_both_engines_in_one_tree_keep_their_own_areas(snaps):
    mk_snap(snaps, "2026-09-08_100000", "completed", layout="rsync")
    mk_snap(snaps, "2026-09-09_100000", "completed")
    by = {s.key: s.origins[0].engine for s in scan(snaps)}
    assert by == {"2026-09-08_100000": "rsync", "2026-09-09_100000": "btrfs"}


# ---- snapshots locales (port de test_scanner.py) -------------------------------------------------------


@pytest.fixture
def local(tmp_path):
    d = tmp_path / ".snapshots"
    (d / "home_parent" / "tester").mkdir(parents=True)
    (d / "root_parent").mkdir()
    (d / "home_20260906_153000" / "tester").mkdir(parents=True)
    (d / "root_20260906_153000").mkdir()
    (d / "1" / "snapshot").mkdir(parents=True)
    (d / "1" / "info.xml").write_text(
        "<snapshot><date>2026-09-06 10:00:00</date>"
        "<description>Pre-upgrade</description><num>1</num></snapshot>"
    )
    return d


def test_scan_local_finds_parents_timestamped_and_snapper(local):
    snaps = scan_local(local, user="tester", home=HOME)
    keys = {s.key for s in snaps}
    assert {"local:home_parent", "local:root_parent", "local:snapper_1"} <= keys
    assert "2026-09-06_153000" in keys
    assert len(snaps) == 5
    hp = next(s for s in snaps if s.key == "local:home_parent")
    assert hp.origins[0].areas[0].path == local / "home_parent" / "tester"
    assert hp.origins[0].kind == "local" and hp.origins[0].label == "Local"
    sn = next(s for s in snaps if s.key == "local:snapper_1")
    assert "Pre-upgrade" in sn.title and sn.timestamp == datetime(2026, 9, 6, 10, 0, 0)


def test_local_timestamped_home_and_root_share_a_key_and_merge(local):
    snaps = scan_local(local, user="tester", home=HOME)
    merged = merge_timeline(snaps)
    one = next(s for s in merged if s.key == "2026-09-06_153000")
    assert len(one.origins) == 2  # el home y el root del mismo instante


def test_broken_parent_symlink_is_skipped(tmp_path):
    d = tmp_path / ".snapshots"
    d.mkdir()
    (d / "home_parent").symlink_to(tmp_path / "gone")
    assert scan_local(d, user="tester", home=HOME) == []


def test_missing_local_dir(tmp_path):
    assert scan_local(tmp_path / "nope", user="tester", home=HOME) == []


# ---- fusión de línea de tiempo --------------------------------------------------------------------------


def snap(key, label, kind="usb", status="completed"):
    return Snapshot(
        key,
        parse_key_time(key),
        status,
        [Origin(label, kind, [Area("home", "Home", Path("/x"), HOME)])],
    )


def test_same_backup_on_usb_and_remote_is_one_entry_with_both_tags():
    out = merge_timeline(
        [snap("2026-09-08_100000", "USB")], [snap("2026-09-08_100000", "Box", "remote")]
    )
    assert len(out) == 1 and out[0].tag == "USB+Box"
    assert out[0].best_origin().label == "USB"  # lo más barato primero


def test_remote_only_entry_stays_and_order_is_newest_first():
    out = merge_timeline(
        [snap("2026-09-08_100000", "USB")], [snap("2026-09-09_100000", "Box", "remote")]
    )
    assert [s.key for s in out] == ["2026-09-09_100000", "2026-09-08_100000"]
    assert out[0].best_origin().kind == "remote"


def test_partial_wins_over_completed_when_merging():
    out = merge_timeline(
        [snap("2026-09-08_100000", "USB", status="completed")],
        [snap("2026-09-08_100000", "Box", "remote", status="partial")],
    )
    assert out[0].status == "partial"


def test_merge_does_not_mutate_its_inputs():
    a, b = snap("2026-09-08_100000", "USB"), snap("2026-09-08_100000", "Box", "remote")
    merge_timeline([a], [b])
    assert len(a.origins) == 1 and len(b.origins) == 1


def test_best_origin_prefers_local_then_usb_then_remote():
    s = Snapshot(
        "k",
        datetime(2026, 1, 1),
        "completed",
        [Origin("R", "remote"), Origin("U", "usb"), Origin("L", "local")],
    )
    assert s.best_origin().label == "L"


def test_key_helpers():
    assert key_from_compact("20260915_001833") == "2026-09-15_001833"
    assert key_from_compact("nonsense") is None
    assert parse_key_time("2026-09-15_001833_2") == datetime(2026, 9, 15, 0, 18, 33)
    assert parse_key_time("zzz") is None


# ---- host remoto btrfs (sin manifest) -----------------------------------------------------------------------


def line(rel, recv="u-1"):
    return f"ID 258 gen 10 top level 5 parent_uuid - received_uuid {recv} path {rel}"


def fake_run(stdout, rc=0, record=None):
    def run(argv, **kw):
        if record is not None:
            record.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    return run


def remote(stdout, tmp_path, rc=0, **kw):
    rec = []
    out = scan_remote_btrfs(
        host="h.example",
        user="u",
        port=2222,
        base="/srv/my backups",
        label="Box",
        local_user="tester",
        home=HOME,
        local_dir=tmp_path / ".snapshots",
        run=fake_run(stdout, rc, rec),
        **kw,
    )
    return out, rec


def test_connected_listing_groups_home_and_root_by_timestamp(tmp_path):
    out = (
        "__CONN_OK__\n"
        + "\n".join(
            [
                line("home/home_20260915_001833"),
                line("root/root_20260915_001833"),
                line("home/home_20260916_001833"),
            ]
        )
        + "\n__SUDO_OK__\n"
    )
    (snaps, status, skipped), _ = remote(out, tmp_path)
    assert status == RemoteStatus.CONNECTED and skipped == 0
    assert [s.key for s in snaps] == ["2026-09-16_001833", "2026-09-15_001833"]
    a = snaps[1].origins[0]
    assert a.kind == "remote" and a.label == "Box" and a.engine == "btrfs"
    assert a.area("home").path == "/srv/my backups/home/home_20260915_001833"
    assert a.area("home").storage is Storage.REMOTE and a.area("root").target_base == Path("/")


def test_half_received_copy_is_not_offered(tmp_path):
    out = "__CONN_OK__\n" + line("home/home_20260915_001833", recv="-") + "\n__SUDO_OK__\n"
    (snaps, status, _), _ = remote(out, tmp_path)
    assert snaps == [] and status == RemoteStatus.CONNECTED


def test_names_outside_the_pattern_and_other_dirs_are_ignored(tmp_path):
    out = (
        "__CONN_OK__\n"
        + "\n".join(
            [
                line("home/home_manual"),
                line("other/home_20260915_001833"),
                line("home/root_20260915_001833"),
                "noise without path",
            ]
        )
        + "\n__SUDO_OK__\n"
    )
    (snaps, _s, _k), _ = remote(out, tmp_path)
    assert snaps == []


def test_no_privilege_falls_back_to_names_only_as_unknown(tmp_path):
    out = (
        "__CONN_OK__\n__SUDO_NO__\nnosudo /srv/my backups/home/home_20260915_001833\n"
        "nosudo /srv/my backups/root/root_20260915_001833\n"
    )
    (snaps, status, _), _ = remote(out, tmp_path)
    assert status == RemoteStatus.NO_PRIVILEGE and len(snaps) == 1
    assert snaps[0].status == "unknown" and len(snaps[0].origins[0].areas) == 2


def test_unreachable_cases(tmp_path):
    for rc, out in ((255, ""), (0, "garbage")):
        (snaps, status, _), _ = remote(out, tmp_path, rc=rc)
        assert snaps == [] and status == RemoteStatus.UNREACHABLE

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired("ssh", 5)

    r = scan_remote_btrfs(
        host="h",
        user="u",
        port=22,
        base="/b",
        label="B",
        local_user="t",
        home=HOME,
        local_dir=tmp_path,
        run=boom,
    )
    assert r == ([], RemoteStatus.UNREACHABLE, 0)


def test_not_configured(tmp_path):
    r = scan_remote_btrfs(
        host="",
        user="u",
        port=22,
        base="",
        label="B",
        local_user="t",
        home=HOME,
        local_dir=tmp_path,
    )
    assert r == ([], RemoteStatus.NONE, 0)


def test_remote_copy_already_local_is_hidden_and_counted(tmp_path):
    local = tmp_path / ".snapshots"
    (local / "home_20260915_001833").mkdir(parents=True)
    out = (
        "__CONN_OK__\n"
        + line("home/home_20260915_001833")
        + "\n"
        + line("root/root_20260916_001833")
        + "\n__SUDO_OK__\n"
    )
    (snaps, _s, skipped), _ = remote(out, tmp_path)
    assert skipped == 1 and [s.key for s in snaps] == ["2026-09-16_001833"]


def test_ssh_argv_is_accept_new_with_one_quoted_remote_command(tmp_path):
    out = "__CONN_OK__\n__SUDO_OK__\n"
    _, rec = remote(out, tmp_path)
    argv = rec[0]
    assert "StrictHostKeyChecking=accept-new" in argv and not any(
        "StrictHostKeyChecking=no" in a for a in argv
    )
    assert argv[argv.index("-p") + 1] == "2222" and "u@h.example" in argv
    cmd = argv[-1]
    assert "sudo -n btrfs subvolume list -r -R '/srv/my backups'" in cmd and "-r -R" in cmd
    assert argv.count(cmd) == 1  # un solo elemento


def test_remote_command_quotes_hostile_base():
    cmd = sources.remote_btrfs_command("/b/x'; rm -rf / #")
    assert "'/b/x'\"'\"'; rm -rf / #'" in cmd
