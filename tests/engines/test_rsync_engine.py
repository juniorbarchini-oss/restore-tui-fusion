"""RsyncHardlinkEngine. Portado de restore-tui (tests/test_backup.py) a pytest.
rsync REAL en directorios temporales; el destino remoto usa un `ssh` falso."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.destinations import LocalDestination, RemoteQueryError, SSHDestination
from restore_fusion.engines import EngineContext, FullCheck, RsyncHardlinkEngine, exclusions_for
from restore_fusion.restore.engine import RestoreEngine
from restore_fusion.model.restore import ConflictResolution
from restore_fusion.model.manifest import SnapshotRecord, pick_baseline
from restore_fusion.safety import RetentionPolicy, apply_deletions, approve_retention, plan_prune

pytestmark = pytest.mark.needs_rsync


def prune(dest, names, plan):
    ap = approve_retention(plan)
    return apply_deletions(names, lambda n: dest.remove(n, ap), ap)


def manifest_of(dest, name):
    return json.loads((dest.snaps / name / "manifest.json").read_text())


def events():
    log = []
    return log, lambda t, m: log.append((t, m))


# ---- completa e incremental ----------------------------------------------------


def test_initial_full_backup(usb, ctx, clock):
    log, emit = events()
    r = RsyncHardlinkEngine().run(usb, ctx, emit)
    assert r.status == "completed" and r.ok and r.baseline is None
    snap = r.snapshot_path
    assert (snap / "home" / "Documents" / "a.txt").read_text() == "hello"
    assert not (snap / "home" / ".cache").exists(), "exclusion failed"
    assert (snap / "_system_state" / "os_info.json").exists()
    assert (snap / "restore.sh").exists() and os.access(snap / "restore.sh", os.X_OK)
    assert (snap / "backup.log").exists()
    m = manifest_of(usb, r.snapshot_name)
    assert m["status"] == "completed" and m["link_dest_baseline"] is None
    assert m["engine"] == "rsync" and m["manifest_version"] == 2
    assert m["hostname"] == "host1" and m["user"] == "tester"
    assert usb.latest.is_symlink() and usb.latest.resolve() == snap.resolve()


def test_incremental_hardlinks_unchanged_files(usb, ctx, home, clock):
    e = RsyncHardlinkEngine()
    r1 = e.run(usb, ctx)
    clock.tick()
    (home / "Documents" / "c.txt").write_text("new file")
    r2 = e.run(usb, ctx)
    assert r2.status == "completed" and r2.baseline == r1.snapshot_name
    a1 = (r1.snapshot_path / "home" / "Documents" / "a.txt").stat()
    a2 = (r2.snapshot_path / "home" / "Documents" / "a.txt").stat()
    assert a1.st_ino == a2.st_ino, "unchanged file was NOT hardlinked to the baseline"
    assert (r2.snapshot_path / "home" / "Documents" / "c.txt").read_text() == "new file"
    assert manifest_of(usb, r2.snapshot_name)["link_dest_baseline"] == r1.snapshot_name
    assert usb.latest.resolve() == r2.snapshot_path.resolve()


# ---- regla 2: parcial / fallido ----------------------------------------------------


def test_partial_run_does_not_move_latest_or_become_baseline(usb, ctx, clock):
    e = RsyncHardlinkEngine()
    e.run(usb, ctx)
    clock.tick()
    r2 = e.run(usb, ctx)
    clock.tick()
    with mock.patch.object(e, "_rsync", return_value=23):
        r3 = e.run(usb, ctx)
    assert r3.status == "partial" and not r3.ok
    assert manifest_of(usb, r3.snapshot_name)["status"] == "partial"
    assert usb.latest.resolve() == r2.snapshot_path.resolve(), "latest moved on partial!"
    assert e.baseline(usb).name == r2.snapshot_name
    clock.tick()
    r4 = e.run(usb, ctx)  # la siguiente corrida NO usa el parcial como baseline
    assert r4.baseline == r2.snapshot_name


@pytest.mark.parametrize("code", [1, 12, 30])
def test_failed_run_does_not_move_latest(usb, ctx, clock, code):
    e = RsyncHardlinkEngine()
    r1 = e.run(usb, ctx)
    clock.tick()
    with mock.patch.object(e, "_rsync", return_value=code):
        r = e.run(usb, ctx)
    assert r.status == "failed" and str(code) in r.message
    assert usb.latest.resolve() == r1.snapshot_path.resolve()
    assert e.baseline(usb).name == r1.snapshot_name


@pytest.mark.parametrize("code", [24, 25])
def test_vanished_files_is_success_with_warning(usb, ctx, code):
    e = RsyncHardlinkEngine()
    with mock.patch.object(e, "_rsync", return_value=code):
        r = e.run(usb, ctx)
    assert r.status == "completed" and r.warnings and "normal on a live home" in r.warnings[0]


def test_interrupt_leaves_a_partial_snapshot_and_reraises(usb, ctx, clock):
    e = RsyncHardlinkEngine()
    r1 = e.run(usb, ctx)
    clock.tick()
    with mock.patch.object(e, "_rsync", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            e.run(usb, ctx)
    newest = usb.list_snapshots()[-1]
    assert manifest_of(usb, newest)["status"] == "partial"
    assert manifest_of(usb, newest)["error"] == "interrupted"
    assert usb.latest.resolve() == r1.snapshot_path.resolve()


def test_running_snapshot_left_by_a_kill_is_never_a_baseline(usb, ctx, clock):
    e = RsyncHardlinkEngine()
    r1 = e.run(usb, ctx)
    name = "2026-03-09_090000"
    usb.reserve_snapshot(name)
    usb.write_manifest(name, {"status": "running", "engine": "rsync"})
    assert e.baseline(usb).name == r1.snapshot_name


def test_unwritable_final_manifest_does_not_complete(usb, ctx):
    e = RsyncHardlinkEngine()
    real = usb.write_manifest
    calls = {"n": 0}

    def flaky(snap, data):
        calls["n"] += 1
        if data.get("status") != "running":
            raise OSError("disk went away")
        return real(snap, data)

    with mock.patch.object(usb, "write_manifest", side_effect=flaky):
        r = e.run(usb, ctx)
    assert r.status == "partial" and not usb.latest.exists()


# ---- historia / baseline / full ------------------------------------------------------


def test_would_be_full(usb, ctx, clock):
    e = RsyncHardlinkEngine()
    assert e.would_be_full(usb) is FullCheck.YES
    e.run(usb, ctx)
    assert e.would_be_full(usb) is FullCheck.NO


def test_other_engine_history_means_full(usb):
    n = usb.reserve_snapshot("2026-01-01_100000")
    usb.write_manifest(n, {"status": "completed", "engine": "btrfs"})
    assert RsyncHardlinkEngine().would_be_full(usb) is FullCheck.YES


def test_unidentifiable_history_means_full(usb):
    n = usb.reserve_snapshot("2026-01-01_100000")
    usb.write_manifest(n, {"status": "completed"})
    assert RsyncHardlinkEngine().would_be_full(usb) is FullCheck.YES


def test_v1_rsync_manifest_from_the_old_edition_is_a_baseline(usb, ctx, clock):
    n = usb.reserve_snapshot("2026-01-01_100000")
    (usb.snaps / n / "home").mkdir()
    usb.write_manifest(
        n,
        {
            "manifest_version": 1,
            "status": "completed",
            "rsync_exit": 0,
            "source_home": "/h",
            "link_dest_baseline": None,
        },
    )
    e = RsyncHardlinkEngine()
    assert e.would_be_full(usb) is FullCheck.NO
    r = e.run(usb, ctx)
    assert r.baseline == n


def test_unknown_when_destination_cannot_be_asked(ctx):
    class Broken(LocalDestination):
        def list_snapshots(self):
            raise RemoteQueryError("ssh down")

    d = Broken(Path("/nonexistent/restore-tui"), is_mount=lambda p: True)
    assert RsyncHardlinkEngine().would_be_full(d) is FullCheck.UNKNOWN


def test_run_does_not_send_blindly_when_history_unreadable(usb, ctx):
    with mock.patch.object(usb, "list_snapshots", side_effect=RemoteQueryError("ssh down")):
        r = RsyncHardlinkEngine().run(usb, ctx)
    assert r.status == "failed" and "not sending" in r.message
    assert not usb.snaps.exists()


# ---- destino no montado --------------------------------------------------------------------


def test_unmounted_destination_writes_nothing(tmp_path, ctx):
    (tmp_path / "usb").mkdir()
    d = LocalDestination(tmp_path / "usb" / "restore-tui", is_mount=lambda p: False)
    r = RsyncHardlinkEngine().run(d, ctx)
    assert r.status == "failed" and "not reachable" in r.message
    assert not (tmp_path / "usb" / "restore-tui").exists()


# ---- dry run ----------------------------------------------------------------------------------


def test_dry_run_writes_nothing(usb, ctx):
    usb.ensure_ready()
    r = RsyncHardlinkEngine().run(usb, ctx, dry_run=True)
    assert r.status == "completed" and r.message == "dry run finished"
    assert usb.list_snapshots() == [] and not usb.latest.exists()


# ---- argv / exclusiones --------------------------------------------------------------------------


def test_exclusions_for():
    base = exclusions_for(True, ["foo/", ".cache/"])
    assert base.count(".cache/") == 1 and base[-1] == "foo/" and "node_modules" in " ".join(base)
    assert exclusions_for(False, ["foo/"]) == ["foo/"]
    assert "Documents/desarrollo/" not in exclusions_for(True, [])


def test_argv_is_a_list_with_link_dest(usb, ctx):
    e = RsyncHardlinkEngine()
    argv = e._argv(usb, ctx, "2026-03-02_100000", "2026-03-01_100000", False)
    assert isinstance(argv, list) and argv[0] == "rsync"
    assert {"-aAX", "--numeric-ids", "-x", "--delete", "--delete-excluded"} <= set(argv)
    assert any(a == f"--link-dest={usb.snaps}/2026-03-01_100000/home" for a in argv)
    assert argv[-2] == f"{ctx.source_home}/" and argv[-1].endswith("2026-03-02_100000/home/")
    assert "-n" in e._argv(usb, ctx, "n", None, True)


# ---- restaurar de lo respaldado ---------------------------------------------------------------------


def test_restore_round_trip(usb, ctx, tmp_path):
    r = RsyncHardlinkEngine().run(usb, ctx)
    snap_home = r.snapshot_path / "home"
    eng = RestoreEngine(user="t", target_uid=os.getuid(), target_gid=os.getgid())
    dest = tmp_path / "restored"
    items = eng.prepare_items([snap_home / "Documents"], snap_home)
    final = list(eng.restore_generator(items, dest, ConflictResolution.OVERWRITE))[-1]
    assert final.ok
    assert (dest / "Documents" / "a.txt").read_text() == "hello"
    assert (dest / "Documents" / "b.txt").read_text() == "world"


# ---- retención con plan + Approval --------------------------------------------------------------------


def test_prune_through_plan_and_approval(usb, ctx, home, clock, approve_all):
    e = RsyncHardlinkEngine()
    names = []
    for i in range(5):
        clock.tick()
        (home / "Documents" / f"x{i}.txt").write_text(str(i))
        names.append(e.run(usb, ctx).snapshot_name)
    recs = e.records(usb)
    plan = plan_prune(recs, RetentionPolicy(min_keep=2, max_snapshots=3), "rsync")
    assert plan.over_cap == names[:2]
    prune(usb, plan.over_cap, plan)
    left = usb.list_snapshots()
    assert left == names[2:]
    assert usb.latest.resolve() == (usb.snaps / names[-1]).resolve()
    # disco lleno: el presupuesto puede llevarse como mucho hasta el piso MIN_KEEP
    plan2 = plan_prune(e.records(usb), RetentionPolicy(min_keep=2), "rsync")
    assert plan2.budget_limit == 1
    prune(usb, plan2.budget_candidates(), plan2)
    assert len(usb.list_snapshots()) == 2 and usb.latest.exists()


def test_stale_partials_older_than_newest_completed_are_pruned(usb, ctx, clock):
    e = RsyncHardlinkEngine()
    clock.tick()
    with mock.patch.object(e, "_rsync", return_value=23):
        old_partial = e.run(usb, ctx).snapshot_name
    clock.tick()
    good = e.run(usb, ctx).snapshot_name
    plan = plan_prune(e.records(usb), RetentionPolicy(min_keep=2), "rsync")
    assert plan.stale == [old_partial]
    prune(usb, plan.stale, plan)
    assert usb.list_snapshots() == [good]


# ---- remoto (ssh falso) ---------------------------------------------------------------------------------


def test_remote_full_then_incremental_with_hardlinks(tmp_path, ctx, home, clock, fake_ssh):
    root = str(tmp_path / "remote" / "restore-tui")
    d = SSHDestination(
        host="h.example", root=root, user="u", label="Remote", tcp_check=lambda h, p: True
    )
    e = RsyncHardlinkEngine()
    r1 = e.run(d, ctx)
    assert r1.status == "completed", r1.message
    snaps = Path(root) / "snapshots"
    assert (snaps / r1.snapshot_name / "home" / "Documents" / "a.txt").read_text() == "hello"
    assert (snaps / r1.snapshot_name / "_system_state" / "os_info.json").exists()
    assert (Path(root) / "latest").is_symlink()
    m = json.loads((snaps / r1.snapshot_name / "manifest.json").read_text())
    assert m["status"] == "completed" and m["destination"] == "Remote" and m["engine"] == "rsync"
    clock.tick()
    (home / "Documents" / "remote_only.txt").write_text("r")
    r2 = e.run(d, ctx)
    assert r2.status == "completed" and r2.baseline == r1.snapshot_name
    ia = (snaps / r1.snapshot_name / "home" / "Documents" / "a.txt").stat().st_ino
    ib = (snaps / r2.snapshot_name / "home" / "Documents" / "a.txt").stat().st_ino
    assert ia == ib, "remote incremental did not hardlink unchanged file"


def test_remote_unreachable_fails_without_sending(tmp_path, ctx, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "1")
    d = SSHDestination(
        host="h.example", root=str(tmp_path / "r"), user="u", tcp_check=lambda h, p: True
    )
    r = RsyncHardlinkEngine().run(d, ctx)
    assert r.status == "failed" and "not reachable" in r.message


# ---- diagnóstico: el mensaje de error incluye lo que rsync dijo ---------------------------------------


def fake_rsync(tmp_path, body):
    p = tmp_path / "fake-rsync"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


NOISY = (
    'echo "sending incremental file list" >&2\n'
    'echo "      1,234  50%  1.00MB/s  0:00:01 (xfr#1, to-chk=3/9)"\n'
    'echo "rsync: [receiver] mkdir \\"/x/y/home\\" failed: No such file or directory (2)" >&2\n'
    'echo "rsync: [sender] link_stat \\"/h/secret\\" failed: Permission denied (13)" >&2\n'
    'echo "rsync: [sender] send_files failed to open \\"/h/x\\": Permission denied (13)" >&2\n'
    'echo "Number of files: 9"\n'
    'echo "rsync error: some files/attrs were not transferred (code 23) at main.c(1338)" >&2\n'
    "exit 23\n"
)


def test_dry_run_failure_message_includes_rsync_stderr(usb, ctx, tmp_path):
    e = RsyncHardlinkEngine(rsync_bin=fake_rsync(tmp_path, NOISY))
    r = e.run(usb, ctx, dry_run=True)
    assert r.status == "failed" and "dry run rsync exit 23" in r.message
    assert "mkdir" in r.message and "No such file or directory" in r.message
    assert (
        "xfr#" not in r.message and "Number of files" not in r.message
    )  # sin progreso ni estadísticas
    assert r.message.count(" | ") == 2, "only the first 3 diagnostic lines are quoted"
    assert len(r.details["rsync_diagnostics"]) == 4  # todas quedan en details para el log


def test_real_run_exit_23_and_failures_also_quote_rsync(usb, ctx, tmp_path):
    r = RsyncHardlinkEngine(rsync_bin=fake_rsync(tmp_path, NOISY)).run(usb, ctx)
    assert r.status == "partial" and "Permission denied" in r.message or "mkdir" in r.message
    boom = 'echo "rsync: connection unexpectedly closed (0 bytes received so far)" >&2\nexit 12\n'
    r = RsyncHardlinkEngine(rsync_bin=fake_rsync(tmp_path, boom)).run(usb, ctx)
    assert r.status == "failed" and "connection unexpectedly closed" in r.message


def test_failure_without_output_says_so(usb, ctx, tmp_path):
    r = RsyncHardlinkEngine(rsync_bin=fake_rsync(tmp_path, "exit 23\n")).run(usb, ctx, dry_run=True)
    assert "rsync printed no diagnostics" in r.message


def test_successful_dry_run_message_is_unchanged(usb, ctx, tmp_path):
    r = RsyncHardlinkEngine(rsync_bin=fake_rsync(tmp_path, "exit 0\n")).run(usb, ctx, dry_run=True)
    assert r.message == "dry run finished"


# ---- dry run sin -A/-X (rsync >= 3.4 lee xattrs de un destino que aún no existe) -----------------------


def _argv(usb, ctx, dry_run):
    e = RsyncHardlinkEngine()
    return e._argv(usb, ctx, "2026-01-01_000000", None, dry_run)


def test_dry_run_argv_has_no_acl_or_xattr_flags_but_real_run_keeps_them(usb, ctx):
    dry, real = _argv(usb, ctx, True), _argv(usb, ctx, False)
    assert "-n" in dry and "-n" not in real
    assert "-aAX" in real and "-a" in dry
    flags = [a for a in dry if a.startswith("-") and not a.startswith("--")]
    assert not any("A" in f or "X" in f for f in flags if f != "-x")
    assert "-aAX" not in dry and "-A" not in dry and "-X" not in dry
    assert "-x" in dry and "--numeric-ids" in dry and "--delete" in dry  # el resto se mantiene


def test_dry_run_still_applies_exclusions_and_link_dest(usb, ctx):
    e = RsyncHardlinkEngine()
    argv = e._argv(usb, ctx, "new", "old", True)
    assert argv.count("--exclude") == 2 and any(a.startswith("--link-dest=") for a in argv)
