"""BackupRunner: estado del sistema -> motor -> kit -> retención. Con rsync REAL (destino local y
remoto vía ssh falso) y con el motor btrfs sobre FakeBtrfsOps."""

import json
import os
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from btrfs_fakes import FakeBtrfsOps
from stubs import StubPM
from restore_fusion.backup.runner import BackupRunner, RunSummary
from restore_fusion.config.schema import Config
from restore_fusion.destinations import LocalDestination, SSHDestination
from restore_fusion.engines import RsyncHardlinkEngine
from restore_fusion.engines.base import EngineResult
from restore_fusion.engines.btrfs import BtrfsSendEngine
from restore_fusion.safety import ask_delete


def mk_runner(cfg, engine, dests, **kw):
    events = []
    r = BackupRunner(
        cfg,
        engine,
        dests,
        StubPM(),
        emit=lambda t, m: events.append((t, m)),
        hostname="host1",
        **kw,
    )
    r.events = events
    return r


def manifest_of(dest, name):
    return json.loads((dest.snaps / name / "manifest.json").read_text())


pytestmark_rsync = pytest.mark.needs_rsync


# ---- rsync: flujo completo --------------------------------------------------------------


@pytest.mark.needs_rsync
def test_rsync_run_writes_snapshot_kit_and_state(cfg, usb, clock):
    r = mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"TARBALL")
    summary = r.run()
    assert summary.ok and summary.status == "completed", summary.message
    res = summary.results[0]
    snap = res.snapshot_path
    assert (snap / "home" / "Documents" / "a.txt").read_text() == "hello"
    assert os.access(snap / "restore.sh", os.X_OK)  # el estado del sistema llegó al snapshot
    assert manifest_of(usb, res.snapshot_name)["engine"] == "rsync"
    root = usb.root
    dr = root / "disaster-recovery.sh"
    assert dr.is_file() and os.access(dr, os.X_OK)
    assert "@@" not in dr.read_text() and "@@" not in (root / "RECOVERY.md").read_text()
    assert res.snapshot_name in (root / "RECOVERY.md").read_text()
    assert (root / "restore-fusion-src.tar.gz").read_bytes() == b"TARBALL"
    assert not (snap / "disaster-recovery.sh").exists()  # el kit está en la raíz, no en el snapshot
    assert subprocess.run(["bash", "-n", str(dr)]).returncode == 0
    assert usb.latest.resolve() == snap.resolve()


@pytest.mark.needs_rsync
def test_no_kit_and_no_retention_after_a_partial_run(cfg, usb, clock):
    e = RsyncHardlinkEngine()
    mk_runner(cfg, e, [usb], tarball=b"x").run()
    kit_before = (usb.root / "RECOVERY.md").read_text()
    clock.tick()
    with mock.patch.object(e, "_rsync", return_value=23):
        s = mk_runner(cfg, e, [usb], tarball=b"x").run()
    assert s.status == "failed" or s.status == "partial"
    assert s.results[0].status == "partial"
    assert (usb.root / "RECOVERY.md").read_text() == kit_before  # no se reescribió
    assert s.pruned == {}


@pytest.mark.needs_rsync
def test_kit_failure_is_a_warning_not_a_failed_backup(cfg, usb, clock):
    with mock.patch.object(usb, "write_kit_files", side_effect=OSError("read-only")):
        s = mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"x").run()
    assert s.ok and any("recovery kit not written" in w for w in s.warnings)


@pytest.mark.needs_rsync
def test_unsupported_distro_still_backs_up_the_files(cfg, usb, clock):
    r = BackupRunner(cfg, RsyncHardlinkEngine(), [usb], None, tarball=b"x")
    s = r.run()
    assert s.ok and any("unsupported distro" in w for w in s.warnings)
    assert (s.results[0].snapshot_path / "home" / "Documents" / "a.txt").exists()


@pytest.mark.needs_rsync
def test_stage_dir_is_removed(cfg, usb, clock, tmp_path):
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("restore-fusion-stage-*"))
    mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"x").run()
    assert set(Path(tempfile.gettempdir()).glob("restore-fusion-stage-*")) == before


# ---- retención: plan + Approval, nunca bajo MIN_KEEP -------------------------------------------


def _runs(cfg, usb, clock, n, e=None, **kw):
    e = e or RsyncHardlinkEngine()
    out = []
    for i in range(n):
        clock.tick()
        (cfg.home / "Documents" / f"f{i}.txt").write_text(str(i))
        s = mk_runner(cfg, e, [usb], tarball=b"x", **kw).run()
        assert s.ok, s.message
        out.append(s)
    return out


@pytest.mark.needs_rsync
def test_hard_cap_prunes_oldest_completed_through_approval(cfg, usb, clock):
    cfg.max_snapshots = 3
    runs = _runs(cfg, usb, clock, 5)
    names = [r.results[0].snapshot_name for r in runs]
    assert usb.list_snapshots() == names[2:]
    assert runs[-1].pruned["USB"] == [names[1]]
    assert usb.latest.resolve() == (usb.snaps / names[-1]).resolve()


@pytest.mark.needs_rsync
def test_disk_pressure_prunes_down_to_min_keep_and_never_the_last(cfg, usb, clock):
    cfg.min_keep = 2
    _runs(cfg, usb, clock, 4)
    clock.tick()
    with mock.patch.object(usb, "should_prune_more", side_effect=lambda n, mk: n > max(1, mk)):
        s = mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"x").run()
    left = usb.list_snapshots()
    assert len(left) == 2 and left[-1] == s.results[0].snapshot_name
    assert usb.latest.exists()


@pytest.mark.needs_rsync
def test_budget_is_not_touched_when_the_drive_is_fine(cfg, usb, clock):
    _runs(cfg, usb, clock, 4)
    assert len(usb.list_snapshots()) == 4  # sin presión de disco no se borra nada


@pytest.mark.needs_rsync
def test_stale_partial_older_than_newest_is_pruned_with_approval(cfg, usb, clock):
    e = RsyncHardlinkEngine()
    clock.tick()
    with mock.patch.object(e, "_rsync", return_value=23):
        old = mk_runner(cfg, e, [usb], tarball=b"x").run().results[0].snapshot_name
    clock.tick()
    s = mk_runner(cfg, e, [usb], tarball=b"x").run()
    assert old in s.pruned["USB"] and old not in usb.list_snapshots()


@pytest.mark.needs_rsync
def test_retention_can_be_switched_off(cfg, usb, clock):
    cfg.max_snapshots = 1
    _runs(cfg, usb, clock, 3, retention=False)
    assert len(usb.list_snapshots()) == 3


@pytest.mark.needs_rsync
def test_retention_error_is_a_warning(cfg, usb, clock):
    cfg.max_snapshots = 1
    _runs(cfg, usb, clock, 2)
    clock.tick()
    with mock.patch.object(LocalDestination, "remove", side_effect=OSError("busy")):
        s = mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"x").run()
    assert s.ok and s.pruned["USB"] == []


@pytest.mark.needs_rsync
def test_dry_run_writes_nothing_and_shows_what_retention_would_prune(cfg, usb, clock):
    cfg.max_snapshots = 2
    runs = _runs(cfg, usb, clock, 3, retention=False)
    names = [r.results[0].snapshot_name for r in runs]
    kit_before = (usb.root / "RECOVERY.md").read_text()
    clock.tick(30)
    r = mk_runner(cfg, RsyncHardlinkEngine(), [usb], tarball=b"x")
    s = r.run(dry_run=True)
    assert s.ok and usb.list_snapshots() == names
    infos = [m for t, m in r.events]
    assert any("would prune (over MAX_SNAPSHOTS)" in m and names[0] in m for m in infos)
    assert (usb.root / "RECOVERY.md").read_text() == kit_before  # --dry-run no reescribe el kit


@pytest.mark.needs_rsync
def test_remote_rsync_run_writes_kit_with_user_and_host(cfg, ssh_dest, clock, fake_ssh):
    s = mk_runner(cfg, RsyncHardlinkEngine(), [ssh_dest], tarball=b"x").run()
    assert s.ok, s.message
    root = Path(ssh_dest.root)
    dr = (root / "disaster-recovery.sh").read_text()
    assert 'HOST="u@h.example"' in dr and f'BASE="{ssh_dest.root}"' in dr and "@@" not in dr
    assert "RESTORE_TUI_WORK" in dr
    assert (root / "RECOVERY.md").exists()


# ---- btrfs (FakeBtrfsOps) -------------------------------------------------------------------------


@pytest.fixture
def bcfg(tmp_path, cfg):
    rootfs, homefs = tmp_path / "rootfs", tmp_path / "homefs"
    for d in (rootfs, homefs):
        d.mkdir()
    (homefs / "note").write_text("hi")
    cfg.source_mounts = [str(rootfs), str(homefs)]
    cfg.local_snapshots_dir = tmp_path / "dot_snapshots"
    cfg.local_snapshots_dir.mkdir()
    cfg.local_keep = 2
    return cfg


def btrfs_engine(cfg, ops=None):
    return BtrfsSendEngine(cfg, ops or FakeBtrfsOps(), is_root=lambda: True)


def test_btrfs_usb_and_remote_run_with_kits(bcfg, tmp_path, clock):
    ops = FakeBtrfsOps()
    (tmp_path / "usb2").mkdir()
    usb = LocalDestination(
        tmp_path / "usb2" / "btrfs-restore", is_mount=lambda p: True, label="USB"
    )
    rem = SSHDestination(
        host="h.example",
        root="/srv/backups",
        user="bob",
        label="Remote",
        tcp_check=lambda h, p: True,
    )
    s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [usb, rem], tarball=b"x").run()
    assert [r.status for r in s.results] == ["completed", "completed"], s.message
    assert (usb.root / "disaster-recovery.sh").is_file()
    assert "Btrfs backups" in (usb.root / "RECOVERY.md").read_text()
    kit = next(f for d, f in ops.pushed_trees if d.rstrip("/") == "/srv/backups")
    assert {"disaster-recovery.sh", "RECOVERY.md", "restore-fusion-src.tar.gz"} <= set(kit)
    script = kit["disaster-recovery.sh"].decode()
    assert (
        'HOST="bob@h.example"' in script and 'BASE="/srv/backups"' in script and "@@" not in script
    )


def test_btrfs_retention_is_plan_plus_approval_everywhere(bcfg, tmp_path, clock):
    ops = FakeBtrfsOps()
    (tmp_path / "usb2").mkdir()
    usb = LocalDestination(
        tmp_path / "usb2" / "btrfs-restore", is_mount=lambda p: True, label="USB"
    )
    bcfg.max_snapshots = 2
    names = []
    for _ in range(4):
        clock.tick()
        s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [usb], tarball=b"x").run()
        assert s.ok, s.message
        names.append(s.results[0].snapshot_name)
    assert usb.list_snapshots() == names[2:]
    # los snapshots locales más allá de LOCAL_KEEP salieron por el plan, y los padres siguen
    last_man = json.loads((usb.snaps / names[-1] / "manifest.json").read_text())
    for n in last_man["local_snapshots"].values():
        assert (bcfg.local_snapshots_dir / n).is_dir()
    assert ops.deleted_local  # se podaron (vía Approval)


def test_btrfs_remote_retention_deletes_only_with_the_plan(bcfg, clock):
    bcfg.local_keep = 2
    ops = FakeBtrfsOps()
    kinds = ["rootfs", "homefs"]
    for k in kinds:
        ops.remote_subvols[k] = [f"{k}_2026010{i}_000000" for i in range(1, 5)]
    rem = SSHDestination(
        host="h.example",
        root="/srv/backups",
        user="bob",
        label="Remote",
        tcp_check=lambda h, p: True,
    )
    s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [rem], tarball=b"x").run()
    assert s.ok, s.message
    # tras enviar, la retención borró lo más viejo (solo nombres con marca de tiempo)
    assert ops.ssh_deleted and all("_2026010" in d for d in ops.ssh_deleted)
    # quedan los 2 más nuevos por kind: el de esta corrida y el 04
    assert not [d for d in ops.ssh_deleted if "_20260104_" in d]
    assert len(ops.ssh_deleted) == 6


def test_btrfs_dry_run_describes_retention_and_writes_nothing(bcfg, tmp_path, clock):
    ops = FakeBtrfsOps()
    (tmp_path / "usb2").mkdir()
    usb = LocalDestination(
        tmp_path / "usb2" / "btrfs-restore", is_mount=lambda p: True, label="USB"
    )
    r = mk_runner(bcfg, btrfs_engine(bcfg, ops), [usb])
    s = r.run(dry_run=True)
    assert s.ok and ops.sent == [] and not usb.root.exists()
    assert any("retention keeps at least" in m for _t, m in r.events)


# ---- RunSummary -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sts,expected",
    [
        ([], "failed"),
        (["completed"], "completed"),
        (["completed", "completed"], "completed"),
        (["completed", "partial"], "partial"),
        (["completed", "failed"], "partial"),
        (["partial"], "partial"),
        (["partial", "failed"], "failed"),
        (["failed"], "failed"),
    ],
)
def test_summary_status(sts, expected):
    s = RunSummary([EngineResult(status=x, destination=f"d{i}") for i, x in enumerate(sts)])
    assert s.status == expected and s.ok is (expected == "completed")


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (phase 1, item B): a USB-only run does not know the SSH baseline, so it is "
    "not protected from LOCAL_KEEP pruning; the next remote send becomes a full send (which the "
    "tool asks to confirm). Fixing it needs the runner to learn the remote baseline without "
    "contacting the server; not done in phase 1.",
)
def test_btrfs_usb_only_runs_do_not_prune_the_ssh_baseline(bcfg, tmp_path, clock):
    """Última corrida solo USB: el padre local que es base del remoto no debe podarse por LOCAL_KEEP,
    o el próximo envío al remoto sería COMPLETO."""
    bcfg.local_keep = 2
    ops = FakeBtrfsOps()
    (tmp_path / "usb2").mkdir()
    usb = LocalDestination(
        tmp_path / "usb2" / "btrfs-restore", is_mount=lambda p: True, label="USB"
    )
    rem = SSHDestination(
        host="h.example",
        root="/srv/backups",
        user="bob",
        label="Remote",
        tcp_check=lambda h, p: True,
    )
    s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [rem], tarball=b"x").run()
    assert s.ok, s.message
    remote_base = {k: list(v) for k, v in ops.remote_subvols.items() if v}
    assert remote_base
    for _ in range(3):  # más de LOCAL_KEEP corridas solo USB
        clock.tick()
        s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [usb], tarball=b"x").run()
        assert s.ok, s.message
    for names in remote_base.values():
        for n in names:
            assert (bcfg.local_snapshots_dir / n).is_dir(), f"remote baseline {n} was pruned"
    assert ops.deleted_local  # la poda sigue funcionando para el resto
    clock.tick()
    ops.ssh_sent.clear()
    s = mk_runner(bcfg, btrfs_engine(bcfg, ops), [rem], tarball=b"x").run()
    assert s.ok, s.message
    assert ops.ssh_sent and all(parent is not None for _k, parent in ops.ssh_sent)
