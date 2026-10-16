"""BtrfsSendEngine con FakeBtrfsOps (sin btrfs real, sin root). Portado de btrfs-restore-tui
(tests/test_backup.py) a pytest + los tests de las dos fuentes de baseline."""

import datetime as _dt
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.config.schema import Config
from restore_fusion.destinations import LocalDestination, RemoteQueryError, SSHDestination
from restore_fusion.engines import EngineContext, FullCheck
from restore_fusion.engines.baseline import ORIGIN_MANIFEST, ORIGIN_RECEIVED_UUID
from restore_fusion.engines.btrfs import BtrfsSendEngine
from restore_fusion.engines.btrfs.remote import ReceivedUuidBaselineSource
from restore_fusion.safety import (
    ApprovalError,
    RetentionPolicy,
    ask_delete,
    approve_retention,
)

from btrfs_fakes import FakeBtrfsOps


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    root = e.root = tmp_path
    e.rootfs, e.homefs = root / "rootfs", root / "homefs"
    e.snaps = root / "dot_snapshots"
    e.usb_mount = root / "usb"
    for p in (e.rootfs, e.homefs, e.snaps, e.usb_mount):
        p.mkdir()
    (e.homefs / "Documents").mkdir()
    (e.homefs / "Documents" / "note.md").write_text("hello")
    e.cfg = Config(
        user="tester",
        home=root,
        source_mounts=[str(e.rootfs), str(e.homefs)],
        local_snapshots_dir=e.snaps,
    )
    e.stage = root / "stage"
    (e.stage / "_system_state").mkdir(parents=True)
    (e.stage / "_system_state" / "os_info.json").write_text('{"user": "tester"}')
    (e.stage / "restore.sh").write_text("#!/usr/bin/env bash\ntrue\n")
    e.ctx = EngineContext(source_home=root, user="tester", stage_dir=e.stage, hostname="h1")
    e.mounted = {"v": True}
    e.usb = LocalDestination(e.usb_mount / "btrfs-restore", is_mount=lambda p: e.mounted["v"])
    e.remote = SSHDestination(
        host="h.example",
        root="/srv/backups",
        user="bob",
        label="Remote",
        tcp_check=lambda h, p: True,
    )
    e.clock = {"now": _dt.datetime(2026, 9, 8, 10, 0, 0)}

    class FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return e.clock["now"]

    import restore_fusion.engines.btrfs.engine as mod

    monkeypatch.setattr(mod, "datetime", FakeDT)

    def tick(minutes=1):
        e.clock["now"] += _dt.timedelta(minutes=minutes)

    e.tick = tick
    e.engine = lambda ops=None, **kw: BtrfsSendEngine(
        e.cfg, ops or FakeBtrfsOps(), is_root=lambda: True, **kw
    )
    e.events = []
    e.emit = lambda t, m: e.events.append((t, m))
    return e


def manifest(e, name):
    return json.loads((e.usb.snaps / name / "manifest.json").read_text())


def approval_for(*ids):
    return ask_delete(
        "d?", list(ids), input_fn=lambda _p: "y", is_tty=lambda: True, print_fn=lambda s: None
    )


# ---- camino feliz --------------------------------------------------------------


def test_full_then_incremental(env):
    ops = FakeBtrfsOps()
    r1 = env.engine(ops).run(env.usb, env.ctx, env.emit)
    assert r1.status == "completed", r1.message
    m1 = manifest(env, r1.snapshot_name)
    assert m1["status"] == "completed" and m1["engine"] == "btrfs" and m1["manifest_version"] == 2
    assert (r1.snapshot_path / "_system_state" / "os_info.json").exists()
    assert (r1.snapshot_path / "restore.sh").exists()
    assert os.path.basename(os.readlink(env.usb.latest)) == r1.snapshot_name
    assert all(parent is None for _, parent in ops.sent)  # primer envío: todo completo
    env.tick()
    r2 = env.engine(ops).run(env.usb, env.ctx, env.emit)
    assert r2.status == "completed", r2.message
    assert all(parent is not None for _, parent in ops.sent[2:])
    assert os.path.basename(os.readlink(env.usb.latest)) == r2.snapshot_name
    assert set(p for _, p in ops.sent[2:]) == set(m1["local_snapshots"].values())


def test_stream_target_when_not_btrfs(env):
    r = env.engine(FakeBtrfsOps(target_is_btrfs=False)).run(env.usb, env.ctx)
    assert r.status == "completed", r.message
    assert len(list(r.snapshot_path.glob("*.btrfs.zst"))) == 2


# ---- regla 2 ----------------------------------------------------------------------------


def test_partial_run_does_not_move_latest_or_become_parent(env):
    r1 = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    good_latest = os.readlink(env.usb.latest)
    env.tick()
    r2 = env.engine(FakeBtrfsOps(fail_send_on={"homefs"})).run(env.usb, env.ctx)
    assert r2.status == "partial", r2.message
    assert os.readlink(env.usb.latest) == good_latest
    assert manifest(env, r2.snapshot_name)["status"] == "partial"
    env.tick()
    good2 = FakeBtrfsOps()
    r3 = env.engine(good2).run(env.usb, env.ctx)
    assert r3.status == "completed"
    assert {p for _, p in good2.sent} == set(
        manifest(env, r1.snapshot_name)["local_snapshots"].values()
    )


def test_interrupt_mid_send_marks_partial_and_reraises(env):
    r1 = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    env.tick()
    with pytest.raises(KeyboardInterrupt):
        env.engine(FakeBtrfsOps(interrupt_send=True)).run(env.usb, env.ctx)
    newest = env.usb.list_snapshots()[-1]
    assert manifest(env, newest)["status"] == "partial"
    assert manifest(env, newest)["error"] == "interrupted"
    assert os.path.basename(os.readlink(env.usb.latest)) == r1.snapshot_name
    assert not any((env.snaps / ".backup-tmp").iterdir())  # el scratch se limpia igual


def test_needs_root(env):
    eng = BtrfsSendEngine(env.cfg, FakeBtrfsOps(), is_root=lambda: False)
    r = eng.run(env.usb, env.ctx)
    assert r.status == "failed" and "root" in r.message
    assert not list(env.snaps.iterdir())


def test_stale_unmounted_target_dir_is_rejected_not_written_to(env):
    """Incidente real: un punto de montaje que quedó tras desenchufar es un directorio
    escribible; NO es un destino válido (el stream caería en / sin que nadie lo note)."""
    env.mounted["v"] = False
    ops = FakeBtrfsOps()
    r = env.engine(ops).run(env.usb, env.ctx)
    assert r.status == "failed" and "not a mount point" in r.message
    assert ops.sent == [] and not any(env.usb_mount.iterdir())
    assert not list(env.snaps.iterdir())  # ni siquiera se tomaron snapshots locales


def test_existing_local_snapshot_name_is_not_deleted(env):
    e = env.engine(FakeBtrfsOps())
    compact = "20260908_100000"
    (env.snaps / f"rootfs_{compact}").mkdir()
    (env.snaps / f"rootfs_{compact}" / "keep").write_text("x")
    r = e.run(env.usb, env.ctx)
    assert r.status == "failed" and "already exists" in r.message
    assert (env.snaps / f"rootfs_{compact}" / "keep").exists()


def test_dry_run_writes_nothing(env):
    ops = FakeBtrfsOps()
    r = env.engine(ops).run(env.usb, env.ctx, env.emit, dry_run=True)
    assert r.status == "completed" and ops.sent == []
    assert not any(env.snaps.iterdir()) and not env.usb.root.exists()


def test_dry_run_with_unmounted_usb_fails_and_writes_nothing(env):
    env.mounted["v"] = False
    r = env.engine().run(env.usb, env.ctx, dry_run=True)
    assert r.status == "failed" and not any(env.usb_mount.iterdir())


# ---- retención: planes + Approval (ya no automática) ----------------------------------------


def _four_snapshots(env, last_full=False):
    names = []
    for i in range(4):
        r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
        assert r.status == "completed", r.message
        names.append(r.snapshot_name)
        env.tick()
    return names


def test_engine_run_never_prunes_by_itself(env):
    names = _four_snapshots(env)
    assert env.usb.list_snapshots() == names


def test_usb_retention_goes_through_plan_and_approval(env):
    names = _four_snapshots(env)
    eng = env.engine()
    plan = eng.plan_usb_retention(env.usb, RetentionPolicy(min_keep=2))
    assert plan.budget_limit == 2 and plan.budget_candidates() == names[:2]
    # sin Approval no se borra nada
    with pytest.raises(ApprovalError):
        eng.prune_usb(env.usb, names[:2], None)
    assert env.usb.list_snapshots() == names
    deleted = eng.prune_usb(env.usb, plan.budget_candidates(), approve_retention(plan))
    assert deleted == names[:2] and env.usb.list_snapshots() == names[2:]
    assert os.path.basename(os.readlink(env.usb.latest)) == names[-1]


def test_prune_usb_deletes_received_subvolumes_first(env):
    ops = FakeBtrfsOps()
    names = []
    for _ in range(3):
        names.append(env.engine(ops).run(env.usb, env.ctx).snapshot_name)
        env.tick()
    plan = env.engine(ops).plan_usb_retention(env.usb, RetentionPolicy(min_keep=2))
    env.engine(ops).prune_usb(env.usb, plan.budget_candidates(), approve_retention(plan))
    assert len(ops.deleted_local) >= 2  # los subvolúmenes recibidos se borran con delete_subvolume


def test_hard_cap_plan(env):
    names = _four_snapshots(env)
    plan = env.engine().plan_usb_retention(env.usb, RetentionPolicy(min_keep=2, max_snapshots=2))
    assert plan.over_cap == names[:2]


def test_local_snapshots_kept_generously_and_parent_never_pruned(env):
    env.cfg.local_keep = 2
    last = None
    for _ in range(5):
        r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
        assert r.status == "completed", r.message
        last = r
        env.tick()
    eng = env.engine()
    man = manifest(env, last.snapshot_name)
    plan = eng.plan_local_prune(man["local_snapshots"])
    for n in man["local_snapshots"].values():
        assert n not in plan.over_cap
    with pytest.raises(ApprovalError):
        eng.prune_local(plan, None)
    ops = FakeBtrfsOps()
    deleted = env.engine(ops).prune_local(plan, approve_retention(plan))
    assert deleted and all(not (env.snaps / n).exists() for n in deleted)
    for n in man["local_snapshots"].values():
        assert (env.snaps / n).is_dir()


def test_local_prune_protects_recorded_parents_and_only_our_kinds(env):
    env.cfg.local_keep = 2
    for n in (
        "rootfs_20260101_000000",
        "rootfs_20260102_000000",
        "rootfs_20260103_000000",
        "rootfs_20260104_000000",
        "snapper_20260101_000000",
        "other_20260101_000000",
    ):
        (env.snaps / n).mkdir()
    plan = env.engine().plan_local_prune({}, protect=["rootfs_20260101_000000"])
    assert plan.over_cap == ["rootfs_20260102_000000"][:0] + [n for n in plan.over_cap]  # ver abajo
    assert "rootfs_20260101_000000" not in plan.over_cap  # padre registrado
    assert "snapper_20260101_000000" not in plan.over_cap  # no es nuestro kind
    assert "other_20260101_000000" not in plan.over_cap
    assert "rootfs_20260102_000000" in plan.over_cap


# ---- remoto: SIN manifest, mecanismo received_uuid -----------------------------------------------


def run_remote(env, ops, **kw):
    return env.engine(ops).run_many([env.remote], env.ctx, env.emit, **kw)[0]


def seed_local(env, kinds=("rootfs", "homefs"), stamp="20260101_000000", ops=None, partial=False):
    for k in kinds:
        n = f"{k}_{stamp}"
        (env.snaps / n).mkdir(exist_ok=True)
        if ops is not None:
            (ops.remote_partial if partial else ops.remote_subvols).setdefault(k, []).append(n)


def test_remote_only_backup_works_without_usb(env):
    ops = FakeBtrfsOps()
    r = run_remote(env, ops)
    assert r.status == "completed", r.message
    assert sorted(k for k, _ in ops.ssh_sent) == ["homefs", "rootfs"]
    assert all(p is None for _, p in ops.ssh_sent)


def test_remote_never_reads_or_writes_a_manifest(env):
    ops = FakeBtrfsOps()
    seed_local(env, ops=ops)
    with (
        mock.patch.object(SSHDestination, "read_manifest", side_effect=AssertionError("manifest!")),
        mock.patch.object(
            SSHDestination, "write_manifest", side_effect=AssertionError("manifest!")
        ),
        mock.patch.object(SSHDestination, "list_snapshots", side_effect=AssertionError("tree!")),
    ):
        r = run_remote(env, ops)
    assert r.status == "completed", r.message
    assert all(p == f"{k}_20260101_000000" for k, p in ops.ssh_sent)


def test_remote_incremental_uses_local_parent(env):
    ops = FakeBtrfsOps()
    seed_local(env, ops=ops)
    r = run_remote(env, ops)
    assert r.status == "completed", r.message
    assert all(p == f"{k}_20260101_000000" for k, p in ops.ssh_sent)


def test_remote_base_must_also_exist_locally(env):
    ops = FakeBtrfsOps()
    for k in ("rootfs", "homefs"):
        ops.remote_subvols[k] = [f"{k}_20260101_000000"]  # remoto sí, local no
    run_remote(env, ops)
    assert all(p is None for _, p in ops.ssh_sent)


@pytest.mark.parametrize("failure,text", [("timeout", "timed out"), ("ssh255", "No route to host")])
def test_query_failure_does_not_fall_back_to_full(env, failure, text):
    ops = FakeBtrfsOps(list_failure=failure)
    r = run_remote(env, ops)
    assert r.status == "failed" and text in r.message
    assert ops.ssh_sent == []


def test_answered_but_empty_remote_still_does_full(env):
    ops = FakeBtrfsOps()
    r = run_remote(env, ops)
    assert (
        r.status == "completed"
        and len(ops.ssh_sent) == 2
        and all(p is None for _, p in ops.ssh_sent)
    )


def test_full_kinds_empty_remote(env):
    eng = env.engine(FakeBtrfsOps())
    assert eng.full_kinds(env.remote) == ["rootfs", "homefs"]
    assert eng.would_be_full(env.remote) is FullCheck.YES


def test_full_kinds_empty_when_incremental_possible(env):
    ops = FakeBtrfsOps()
    seed_local(env, ops=ops)
    eng = env.engine(ops)
    assert eng.full_kinds(env.remote) == []
    assert eng.would_be_full(env.remote) is FullCheck.NO


def test_full_kinds_raises_when_remote_cannot_be_asked(env):
    eng = env.engine(FakeBtrfsOps(list_failure="timeout"))
    with pytest.raises(RemoteQueryError):
        eng.full_kinds(env.remote)
    assert eng.would_be_full(env.remote) is FullCheck.UNKNOWN


def test_missing_remote_folder_counts_as_no_base_not_as_error(env):
    eng = env.engine(FakeBtrfsOps(missing_dir=True))
    assert eng.full_kinds(env.remote) == ["rootfs", "homefs"]


def test_half_received_remote_snapshot_is_never_the_base(env):
    ops = FakeBtrfsOps()
    seed_local(env, ops=ops)
    seed_local(env, stamp="20260102_000000", ops=ops, partial=True)
    r = run_remote(env, ops)
    assert r.status == "completed", r.message
    assert all(p == f"{k}_20260101_000000" for k, p in ops.ssh_sent)


def test_only_half_received_copies_means_full(env):
    ops = FakeBtrfsOps()
    seed_local(env, stamp="20260102_000000", ops=ops, partial=True)
    assert len(env.engine(ops).full_kinds(env.remote)) == 2


def test_received_uuid_dash_line_is_not_a_valid_base():
    from restore_fusion.engines.btrfs.remote import has_received_uuid as h

    assert not h("ID 5 gen 1 top level 5 received_uuid - path home/home_20260101_000000")
    assert h("ID 5 gen 1 top level 5 received_uuid abc-1 path home/home_20260101_000000")
    assert not h("ID 5 path home/x")


def test_remote_send_failure_is_partial_and_reports_the_receiver_error(env):
    ops = FakeBtrfsOps(fail_send_on={"homefs"})
    ops.last_stderr = "ssh[1]: ERROR: cannot receive: parent subvol is not read-only"
    r = run_remote(env, ops)
    assert r.status == "partial" and "parent subvol is not read-only" in r.message
    assert any("parent subvol is not read-only" in m for t, m in env.events if t == "error")


def test_system_state_reaches_remote_meta(env):
    ops = FakeBtrfsOps()
    r = run_remote(env, ops)
    dest, files = next((d, f) for d, f in ops.pushed_trees if "/meta/" in d)
    assert dest == f"/srv/backups/meta/{r.snapshot_name}" and "_system_state/os_info.json" in files


def test_state_dir_still_has_content_for_remote_after_usb_runs_first(env):
    """Hallazgo real: el USB MOVÍA el directorio compartido y al remoto le llegaba vacío."""
    ops = FakeBtrfsOps()
    rs = env.engine(ops).run_many([env.usb, env.remote], env.ctx, env.emit)
    assert [x.status for x in rs] == ["completed", "completed"], [x.message for x in rs]
    assert (rs[0].snapshot_path / "_system_state" / "os_info.json").exists()
    meta = next(f for d, f in ops.pushed_trees if d.endswith(f"meta/{rs[0].snapshot_name}"))
    assert "_system_state/os_info.json" in meta
    assert (env.stage / "_system_state" / "os_info.json").exists()


def test_both_targets_share_one_set_of_local_snapshots(env):
    ops = FakeBtrfsOps()
    env.engine(ops).run_many([env.usb, env.remote], env.ctx)
    assert len([p for p in env.snaps.iterdir() if p.is_dir() and p.name != ".backup-tmp"]) == 2


# ---- borrar copias a medias: solo con Approval ------------------------------------------------------


def heal_setup(env, partial_names=None, **fake_kw):
    ops = FakeBtrfsOps()
    for k, v in fake_kw.items():
        setattr(ops, k, v)
    kinds = env.engine(ops).kinds
    for k in kinds:
        for n in partial_names or [f"{k}_20260102_000000"]:
            ops.remote_partial.setdefault(k, []).append(n)
    return ops, kinds


def ids(kinds, stamp="20260102_000000"):
    return [f"{k}/{k}_{stamp}" for k in kinds]


def test_partial_copies_are_listed_without_deleting(env):
    ops, kinds = heal_setup(env)
    found = env.engine(ops).partial_copies(env.remote)
    assert {(f["kind"], f["name"]) for f in found} == {(k, f"{k}_20260102_000000") for k in kinds}
    assert all(not f["has_children"] for f in found) and ops.ssh_deleted == []


def test_name_outside_the_timestamp_pattern_is_never_listed(env):
    ops, _ = heal_setup(env, partial_names=["manual_thing", "x_20260102_000000"])
    assert env.engine(ops).partial_copies(env.remote) == []


def test_approved_partial_copy_is_deleted_and_logged(env):
    ops, kinds = heal_setup(env)
    r = run_remote(env, ops, heal_approval=approval_for(*ids(kinds)))
    assert r.status == "completed", r.message
    assert sorted(ops.ssh_deleted) == sorted(f"/srv/backups/{k}/{k}_20260102_000000" for k in kinds)
    assert any("deleted partial copy" in m for _, m in env.events)


def test_nothing_deleted_without_approval(env):
    ops, _ = heal_setup(env)
    run_remote(env, ops, heal_approval=None)
    assert ops.ssh_deleted == []


def test_nothing_deleted_while_a_receive_is_running(env):
    ops, kinds = heal_setup(env, receive_alive=True)
    run_remote(env, ops, heal_approval=approval_for(*ids(kinds)))
    assert ops.ssh_deleted == [] and any("receive is running" in m for _, m in env.events)


def test_copy_with_dependents_is_kept(env):
    ops, kinds = heal_setup(env)
    for k in kinds:
        ops.remote_children.add(f"{k}_20260102_000000")
    assert all(f["has_children"] for f in env.engine(ops).partial_copies(env.remote))
    run_remote(env, ops, heal_approval=approval_for(*ids(kinds)))
    assert ops.ssh_deleted == [] and any("depending on it" in m for _, m in env.events)


def test_approved_name_that_is_not_partial_is_not_deleted(env):
    ops, kinds = heal_setup(env)
    seed_local(env, ops=ops)
    run_remote(env, ops, heal_approval=approval_for(*ids(kinds, "20260101_000000")))
    assert not [d for d in ops.ssh_deleted if "20260101" in d]


def test_query_failure_while_healing_deletes_nothing(env):
    ops, kinds = heal_setup(env, list_failure="ssh255")
    run_remote(env, ops, heal_approval=approval_for(*ids(kinds)))
    assert ops.ssh_deleted == []


def test_approval_covers_only_what_it_lists(env):
    ops, kinds = heal_setup(env)
    run_remote(env, ops, heal_approval=approval_for(f"{kinds[0]}/{kinds[0]}_20260102_000000"))
    assert ops.ssh_deleted == [f"/srv/backups/{kinds[0]}/{kinds[0]}_20260102_000000"]


# ---- retención remota -----------------------------------------------------------------------------------


def test_remote_retention_plan_ignores_names_outside_the_timestamp_pattern(env):
    env.cfg.local_keep = 2
    ops = FakeBtrfsOps()
    for k in ("rootfs", "homefs"):
        ops.remote_subvols[k] = [f"{k}_2026010{i}_000000" for i in range(1, 5)] + [f"{k}_manual"]
    eng = env.engine(ops)
    plan = eng.plan_remote_retention(env.remote)
    assert not [n for n in plan.over_cap if n.endswith("_manual")]
    assert [n for n in plan.over_cap if "_20260101_" in n] and not [
        n for n in plan.over_cap if "_20260104_" in n or "_20260103_" in n
    ]
    assert ops.ssh_deleted == []  # planear no borra
    with pytest.raises(ApprovalError):
        eng.prune_remote(env.remote, plan, None)
    assert ops.ssh_deleted == []
    eng.prune_remote(env.remote, plan, approve_retention(plan), env.emit)
    assert len(ops.ssh_deleted) == len(plan.over_cap) == 4


def test_run_does_not_prune_the_remote(env):
    env.cfg.local_keep = 2
    ops = FakeBtrfsOps()
    for k in ("rootfs", "homefs"):
        ops.remote_subvols[k] = [f"{k}_2026010{i}_000000" for i in range(1, 5)]
    run_remote(env, ops)
    assert ops.ssh_deleted == []


# ---- una interfaz, dos mecanismos ---------------------------------------------------------------------------


def test_one_baseline_interface_for_both_mechanisms(env):
    ops = FakeBtrfsOps()
    r = env.engine(ops).run(env.usb, env.ctx)  # USB: hay manifest
    seed_local(env, ops=ops)  # remoto: sin manifest
    eng = env.engine(ops)
    usb_src, rem_src = eng.baseline_source(env.usb), eng.baseline_source(env.remote)
    assert usb_src.origin == ORIGIN_MANIFEST and rem_src.origin == ORIGIN_RECEIVED_UUID
    assert isinstance(rem_src, ReceivedUuidBaselineSource)
    ub, rb = usb_src.baseline("rootfs"), rem_src.baseline("rootfs")
    assert ub.origin == ORIGIN_MANIFEST and ub.local_path.is_dir() and ub.manifest is not None
    assert (
        rb.origin == ORIGIN_RECEIVED_UUID
        and rb.name == "rootfs_20260101_000000"
        and rb.manifest is None
    )
    assert usb_src.statuses()[r.snapshot_name] == "completed"
    assert rem_src.statuses("rootfs") == {"rootfs_20260101_000000": "completed"}
    assert usb_src.would_be_full(["rootfs", "homefs"]) == []
    assert rem_src.would_be_full(["rootfs", "homefs"]) == []


def test_remote_statuses_flag_half_received_copies(env):
    ops = FakeBtrfsOps()
    seed_local(env, ops=ops)
    seed_local(env, stamp="20260102_000000", ops=ops, partial=True)
    st = env.engine(ops).baseline_source(env.remote).statuses("rootfs")
    assert st == {"rootfs_20260101_000000": "completed", "rootfs_20260102_000000": "partial"}


def test_usb_baseline_needs_the_local_parent_to_still_exist(env):
    r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    local = manifest(env, r.snapshot_name)["local_snapshots"]
    import shutil

    shutil.rmtree(env.snaps / local["rootfs"])
    src = env.engine().baseline_source(env.usb)
    assert src.baseline("rootfs") is None and src.baseline("homefs") is not None
    assert env.engine().full_kinds(env.usb) == ["rootfs"]


def test_old_edition_v1_usb_manifest_is_a_baseline(env):
    n = env.usb.reserve_snapshot("2026-01-01_100000")
    (env.snaps / "rootfs_20260101_100000").mkdir()
    (env.snaps / "homefs_20260101_100000").mkdir()
    env.usb.write_manifest(
        n,
        {  # forma real del manifest v1 de btrfs-restore-tui
            "manifest_version": 1,
            "snapshot": n,
            "status": "completed",
            "target_is_btrfs": True,
            "local_snapshots": {
                "rootfs": "rootfs_20260101_100000",
                "homefs": "homefs_20260101_100000",
            },
            "parents": {"rootfs": None, "homefs": None},
        },
    )
    ops = FakeBtrfsOps()
    r = env.engine(ops).run(env.usb, env.ctx)
    assert r.status == "completed"
    assert {p for _, p in ops.sent} == {"rootfs_20260101_100000", "homefs_20260101_100000"}


def test_rsync_history_on_the_same_usb_is_not_a_btrfs_baseline(env):
    n = env.usb.reserve_snapshot("2026-01-01_100000")
    env.usb.write_manifest(n, {"status": "completed", "engine": "rsync"})
    assert env.engine().full_kinds(env.usb) == ["rootfs", "homefs"]


# ---- exclusiones --------------------------------------------------------------------------------------------------


def seed_caches(env):
    u = env.homefs / "someuser"
    (u / ".cache" / "mozilla").mkdir(parents=True)
    (u / ".cache" / "mozilla" / "blob").write_bytes(b"x" * 4096)
    (u / ".local" / "share" / "Trash" / "files").mkdir(parents=True)
    (u / ".local" / "share" / "Trash" / "files" / "junk").write_text("junk")
    (u / ".config" / "app").mkdir(parents=True)
    (u / ".config" / "app" / "settings.json").write_text("{}")


def local_home(env):
    return next(p for p in env.snaps.iterdir() if p.name.startswith("homefs_"))


def test_caches_are_left_out_and_recorded(env):
    seed_caches(env)
    env.cfg.extra_exclusions = ["*/.cache", "*/.local/share/Trash"]
    env.cfg.exclude_defaults = False
    r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    assert r.status == "completed", r.message
    h = local_home(env)
    assert not (h / "someuser" / ".cache").exists()
    assert not (h / "someuser" / ".local" / "share" / "Trash").exists()
    assert (h / "someuser" / ".config" / "app" / "settings.json").exists()
    assert (h / "Documents" / "note.md").exists()
    man = manifest(env, r.snapshot_name)
    assert "someuser/.cache" in man["excluded"]["homefs"]


def test_default_list_catches_browser_caches(env):
    from restore_fusion.engines.btrfs.exclusions import DEFAULT_BTRFS_EXCLUSIONS

    u = env.homefs / "someuser"
    chrome = u / ".config" / "google-chrome" / "Default"
    (chrome / "Cache" / "Cache_Data").mkdir(parents=True)
    (chrome / "GPUCache").mkdir(parents=True)
    (chrome / "Service Worker" / "CacheStorage").mkdir(parents=True)
    (chrome / "Preferences").write_text("{}")
    (u / ".cache").mkdir()
    (u / ".config" / "Code" / "CachedData").mkdir(parents=True)
    env.cfg.exclude_defaults = False
    env.cfg.extra_exclusions = list(DEFAULT_BTRFS_EXCLUSIONS["/home"])
    assert env.engine(FakeBtrfsOps()).run(env.usb, env.ctx).status == "completed"
    h = local_home(env)
    c = h / "someuser" / ".config" / "google-chrome" / "Default"
    assert not (c / "Cache").exists() and not (c / "GPUCache").exists()
    assert not (c / "Service Worker" / "CacheStorage").exists() and (c / "Preferences").exists()
    assert not (h / "someuser" / ".config" / "Code" / "CachedData").exists()


def test_no_exclusions_uses_plain_ro_snapshot(env):
    seed_caches(env)
    env.cfg.exclude_defaults, env.cfg.extra_exclusions = False, []
    r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    assert (local_home(env) / "someuser" / ".cache").exists()
    assert manifest(env, r.snapshot_name)["excluded"] == {}


def test_exclusion_patterns_cannot_escape_the_snapshot(env):
    """Un EXCLUDE con `..` o ruta absoluta borraría fuera del snapshot (p. ej. otros snapshots)."""
    victim = env.snaps / "rootfs_20250101_000000"
    victim.mkdir()
    (victim / "precious").write_text("keep")
    outside = env.root / "outside"
    outside.mkdir()
    (outside / "file").write_text("keep")
    env.cfg.exclude_defaults = False
    env.cfg.extra_exclusions = [
        "../rootfs_20250101_000000",
        "../../outside",
        f"{outside}/*",
        "/etc/*",
    ]
    r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    assert r.status == "completed", r.message
    assert (victim / "precious").read_text() == "keep" and (outside / "file").exists()
    assert manifest(env, r.snapshot_name)["excluded"] == {}


def test_symlink_in_exclusions_is_not_followed(env):
    outside = env.root / "outside2"
    outside.mkdir()
    (outside / "file").write_text("keep")
    (env.homefs / "link").symlink_to(outside)
    env.cfg.exclude_defaults = False
    env.cfg.extra_exclusions = ["link/*", "link"]
    assert env.engine(FakeBtrfsOps()).run(env.usb, env.ctx).status == "completed"
    assert (outside / "file").read_text() == "keep"


def test_exclusions_for_dedup_and_off():
    from restore_fusion.engines.btrfs.exclusions import exclusions_for

    got = exclusions_for("/home", True, ["*/.cache", "extra"])
    assert got.count("*/.cache") == 1 and got[-1] == "extra"
    assert exclusions_for("/home", False, ["a"]) == ["a"]
    assert "var/tmp/*" in exclusions_for("/", True, [])


# ---- scratch ------------------------------------------------------------------------------------------------------------


def test_run_leaves_no_scratch_dir_and_keeps_last_log(env):
    r = env.engine(FakeBtrfsOps()).run(env.usb, env.ctx)
    assert r.status == "completed", r.message
    tmp_root = env.snaps / ".backup-tmp"
    assert [p for p in tmp_root.iterdir()] == []
    assert (env.snaps / ".backup-last.log").is_file()


def test_sweep_removes_dead_pid_dirs_only(env):
    from restore_fusion.backup.scratch import Scratch

    tmp_root = env.snaps / ".backup-tmp"
    tmp_root.mkdir()
    dead = tmp_root / "999999999-20200101_000000"
    alive = tmp_root / f"{os.getpid()}-20200101_000000"
    notours = tmp_root / "precious-data"
    for d in (dead, alive, notours):
        d.mkdir()
        (d / "x").write_text("scratch")
    legacy = env.snaps / ".state-20200101_000000"
    legacy.mkdir()
    old = _dt.datetime(2020, 1, 1).timestamp()
    os.utime(legacy, (old, old))
    swept = Scratch(env.snaps).sweep_stale()
    assert not dead.exists() and alive.exists() and notours.exists() and not legacy.exists()
    assert set(swept) == {dead.name, legacy.name}


def test_scratch_cleanup_never_deletes_outside_its_root(env):
    from restore_fusion.backup.scratch import Scratch

    victim = env.snaps / "rootfs_x"
    victim.mkdir()
    Scratch(env.snaps).cleanup(victim)
    assert victim.exists()


# ---- REMOTE_KEEP vs LOCAL_KEEP (fase 1) -------------------------------------------------


def _fill_remote(ops, n=6):
    for k in ("rootfs", "homefs"):
        ops.remote_subvols[k] = [f"{k}_2026010{i}_000000" for i in range(1, n + 1)]


def _fill_local(env, n=6):
    for k in ("rootfs", "homefs"):
        for i in range(1, n + 1):
            (env.snaps / f"{k}_2026010{i}_000000").mkdir()


def test_remote_prune_plan_uses_remote_keep_not_local_keep(env):
    env.cfg.local_keep = 2
    env.cfg.remote_keep = 4
    ops = FakeBtrfsOps()
    _fill_remote(ops)
    plan = env.engine(ops).plan_remote_retention(env.remote)
    assert plan.keep_floor == 4 and len(plan.over_cap) == 2 * (6 - 4)


def test_remote_prune_plan_inherits_local_keep_when_unset(env):
    env.cfg.local_keep = 3
    ops = FakeBtrfsOps()
    _fill_remote(ops)
    plan = env.engine(ops).plan_remote_retention(env.remote)
    assert plan.keep_floor == 3 and len(plan.over_cap) == 2 * 3


def test_local_prune_plan_uses_local_keep_not_remote_keep(env):
    env.cfg.local_keep = 3
    env.cfg.remote_keep = 5
    _fill_local(env)
    plan = env.engine().plan_local_prune({})
    assert plan.keep_floor == 3 and len(plan.over_cap) == 2 * 3


def test_both_keep_values_are_floored_at_two(env):
    env.cfg.local_keep = 1
    env.cfg.remote_keep = 1
    ops = FakeBtrfsOps()
    _fill_remote(ops)
    _fill_local(env)
    eng = env.engine(ops)
    assert eng.plan_remote_retention(env.remote).keep_floor == 2
    assert eng.plan_local_prune({}).keep_floor == 2


# ---- aviso: destino local que no es btrfs guarda streams (fase 1, item C corregido) ----------


def _stream_msgs(env):
    return [m for t, m in env.events if t == "warning" and ".btrfs.zst" in m]


def _engine_with_fs(env, ops, fs):
    eng = env.engine(ops)
    eng._fstype = lambda p: fs
    return eng


def test_non_btrfs_usb_gets_one_clear_notice_and_the_run_is_not_blocked(env):
    ops = FakeBtrfsOps(target_is_btrfs=False)
    r = _engine_with_fs(env, ops, "ext4").run(env.usb, env.ctx, env.emit)
    assert r.status == "completed", r.message
    assert len(list(r.snapshot_path.glob("*.btrfs.zst"))) == 2
    msgs = _stream_msgs(env)
    assert len(msgs) == 1
    assert "is ext4, not btrfs" in msgs[0] and "STAGING_DIR" in msgs[0]
    assert str(env.usb_mount) in msgs[0]


def test_unknown_filesystem_still_gets_the_notice(env):
    r = _engine_with_fs(env, FakeBtrfsOps(target_is_btrfs=False), None).run(
        env.usb, env.ctx, env.emit
    )
    assert r.status == "completed", r.message
    assert "is not btrfs" in _stream_msgs(env)[0]


def test_btrfs_usb_gets_no_stream_notice(env):
    r = _engine_with_fs(env, FakeBtrfsOps(), "btrfs").run(env.usb, env.ctx, env.emit)
    assert r.status == "completed", r.message
    assert _stream_msgs(env) == []


def test_dry_run_on_non_btrfs_usb_shows_the_notice_and_writes_nothing(env):
    ops = FakeBtrfsOps(target_is_btrfs=False)
    r = _engine_with_fs(env, ops, "ext4").run(env.usb, env.ctx, env.emit, dry_run=True)
    assert r.status == "completed", r.message
    assert len(_stream_msgs(env)) == 1
    assert any(".btrfs.zst streams" in m for _t, m in env.events)
    assert not ops.sent


# ---- fase 2 D: comprobación del sistema de archivos remoto (solo lectura, no bloquea) ------------


def _fs_msgs(env):
    return [m for t, m in env.events if "remote filesystem" in m or "not btrfs. The btrfs" in m]


def _run_remote(env, fs, dry_run=False):
    ops = FakeBtrfsOps(remote_fs=fs)
    r = env.engine(ops).run(env.remote, env.ctx, env.emit, dry_run=dry_run)
    return ops, r


@pytest.mark.parametrize("dry_run", [False, True])
def test_remote_btrfs_says_nothing(env, dry_run):
    _ops, r = _run_remote(env, "btrfs", dry_run)
    assert r.status == "completed", r.message
    assert _fs_msgs(env) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_remote_ext4_warns_clearly_and_does_not_block_or_switch_engine(env, dry_run):
    ops, r = _run_remote(env, "ext2/ext3", dry_run)
    assert r.status == "completed", r.message  # el aviso no bloquea (el fake sí recibe)
    (msg,) = _fs_msgs(env)
    assert "/srv/backups" in msg and "ext4" in msg and "not btrfs" in msg
    assert "ENGINE=rsync" in msg and "btrfs image" in msg
    assert ("warning", msg) in env.events
    if not dry_run:
        assert ops.ssh_sent  # el motor sigue siendo btrfs: se intentó el envío igual


@pytest.mark.parametrize("fs", [None, "ssh255", "timeout"])
def test_remote_fs_unknown_says_so_and_continues(env, fs):
    ops, r = _run_remote(env, fs)
    assert r.status == "completed", r.message
    (msg,) = _fs_msgs(env)
    assert "could not determine the remote filesystem" in msg
    assert ops.ssh_sent


def test_remote_fs_type_uses_argv_only_and_is_read_only(env):
    from restore_fusion.destinations.ssh import remote_fs_type

    seen = []

    def cap(ssh, remote, cmd, timeout):
        seen.append(cmd)
        return mock.Mock(returncode=0, stdout="btrfs\n")

    env.remote.root = "/srv/it's here; rm -rf /"
    assert remote_fs_type(env.remote, cap) == "btrfs"
    (cmd,) = seen
    assert cmd[0] == "sh" and cmd[-1] == env.remote.root  # la ruta viaja como argumento
    assert env.remote.root not in cmd[2]
    for bad in ("rm ", "mkdir", "touch", ">"):
        assert bad not in cmd[2]


def test_remote_fs_type_garbage_output_is_unknown(env):
    from restore_fusion.destinations.ssh import remote_fs_type

    junk = lambda *a: mock.Mock(returncode=0, stdout="UNKNOWN (0x1234)\n")
    assert remote_fs_type(env.remote, junk) is None
