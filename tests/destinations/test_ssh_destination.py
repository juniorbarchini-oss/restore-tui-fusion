"""SSHDestination con un `ssh` falso que ejecuta localmente (prueba el citado real)."""

import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.destinations import RemoteQueryError, SSHDestination, build_ssh_argv
from restore_fusion.safety.approval import ApprovalError

SNAP = "2026-01-01_100000"
ROOT_DIR = Path(__file__).resolve().parents[2]


def mk(tmp_path, root=None, **kw):
    root = root or str(tmp_path / "remote" / "restore-tui")
    kw.setdefault("tcp_check", lambda h, p: True)
    return SSHDestination(
        host="wan.example", host_lan=kw.pop("host_lan", "lan.example"), root=root, user="u", **kw
    )


# ---- opciones ssh -----------------------------------------------------------------


def test_ssh_argv_always_accept_new_never_no():
    argv = build_ssh_argv("u@h", 2222)
    assert "StrictHostKeyChecking=accept-new" in argv and "BatchMode=yes" in argv
    assert argv[argv.index("-p") + 1] == "2222" and argv[-1] == "u@h"
    assert not any("StrictHostKeyChecking=no" in a for a in argv)


def test_no_strict_host_key_checking_no_anywhere_in_the_package():
    bad = re.compile(r"StrictHostKeyChecking[= ]+(no|off|false)\b", re.I)
    for f in (ROOT_DIR / "restore_fusion").rglob("*.py"):
        assert not bad.search(f.read_text()), f


def test_identity_and_known_hosts_of_the_real_user(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("k")
    (ssh / "known_hosts").write_text("h")
    d = mk(tmp_path, home=tmp_path)
    argv = d.ssh_argv()
    assert str(ssh / "id_ed25519") in argv and f"UserKnownHostsFile={ssh / 'known_hosts'}" in argv


def test_missing_key_files_are_not_added(tmp_path):
    argv = mk(tmp_path, home=tmp_path).ssh_argv()
    assert "-i" not in argv and not any("UserKnownHostsFile" in a for a in argv)


# ---- ruta LAN / WAN -------------------------------------------------------------------


def test_lan_first_then_wan(tmp_path):
    d = mk(tmp_path, tcp_check=lambda h, p: h == "lan.example")
    d._resolve_host()
    assert d.host == "lan.example" and d.route_kind() == "LAN"
    d = mk(tmp_path, tcp_check=lambda h, p: h == "wan.example")
    d._resolve_host()
    assert d.host == "wan.example" and d.route_kind() == "WAN"


def test_nothing_answers_uses_last_candidate(tmp_path):
    d = mk(tmp_path, tcp_check=lambda h, p: False)
    assert d._resolve_host() == "wan.example"


def test_force_route(tmp_path):
    d = mk(tmp_path)
    d.force_route("wan")
    assert d._resolve_host() == "wan.example"
    d.force_route("lan")
    assert d._resolve_host() == "lan.example"
    d.force_route("auto")
    assert d._resolve_host() == "lan.example"


def test_no_lan_configured(tmp_path):
    d = mk(tmp_path, host_lan="")
    assert d._resolve_host() == "wan.example" and d.route_kind() == "WAN"


def test_no_host_means_not_reachable(tmp_path):
    d = SSHDestination(host="", root="/r", user="u")
    assert d.reachable() == (False, "remote target not configured")


# ---- consultas: "no pude preguntar" NO es "vacío" -----------------------------------


def test_list_missing_dir_is_empty_because_the_remote_answered(tmp_path, fake_ssh):
    assert mk(tmp_path).list_snapshots() == []


def test_list_sorted_and_filtered(tmp_path, fake_ssh):
    d = mk(tmp_path)
    for n in (SNAP, "2025-12-31_235959", "junk"):
        (Path(d.snaps) / n).mkdir(parents=True)
    assert d.list_snapshots() == ["2025-12-31_235959", SNAP]


def test_ssh_failure_raises_instead_of_empty(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "1")
    d = mk(tmp_path)
    with pytest.raises(RemoteQueryError):
        d.list_snapshots()
    with pytest.raises(RemoteQueryError):
        d.read_manifest(SNAP)


def test_timeout_raises_instead_of_empty(tmp_path):
    d = mk(tmp_path)
    with mock.patch(
        "restore_fusion.destinations.ssh.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ssh", 5),
    ):
        with pytest.raises(RemoteQueryError, match="timed out"):
            d.list_snapshots()


def test_missing_manifest_is_empty_dict_when_remote_answers(tmp_path, fake_ssh):
    d = mk(tmp_path)
    d.reserve_snapshot(SNAP)
    assert d.read_manifest(SNAP) == {}


def test_manifest_roundtrip(tmp_path, fake_ssh):
    d = mk(tmp_path)
    n = d.reserve_snapshot(SNAP)
    d.write_manifest(n, {"status": "running", "x": ["é", "a b"]})
    assert d.read_manifest(n) == {"status": "running", "x": ["é", "a b"]}


def test_reserve_collisions(tmp_path, fake_ssh):
    d = mk(tmp_path)
    assert d.reserve_snapshot(SNAP) == SNAP
    assert d.reserve_snapshot(SNAP) == SNAP + "_2"


# ---- citado: el comando remoto se arma con shlex.join ------------------------------------


def test_odd_remote_paths_survive_the_shell(tmp_path, fake_ssh):
    odd = tmp_path / 're mote\'s "dir" $HOME; `x`' / "restore-tui"
    d = mk(tmp_path, root=str(odd))
    n = d.reserve_snapshot(SNAP)
    d.write_manifest(n, {"status": "completed"})
    assert (odd / "snapshots" / SNAP / "manifest.json").exists()
    assert d.list_snapshots() == [SNAP]
    assert d.read_manifest(SNAP) == {"status": "completed"}


def test_run_cmd_argument_is_never_interpreted(tmp_path, fake_ssh):
    marker = tmp_path / "pwned"
    d = mk(tmp_path)
    res = d.run_cmd(["echo", f"; touch {marker}"])
    assert res.stdout.strip() == f"; touch {marker}" and not marker.exists()


# ---- latest / remove ------------------------------------------------------------------------


def test_latest_only_on_completed(tmp_path, fake_ssh):
    d = mk(tmp_path)
    n = d.reserve_snapshot(SNAP)
    d.write_manifest(n, {"status": "partial"})
    with pytest.raises(ValueError):
        d.update_latest(n)
    d.write_manifest(n, {"status": "completed"})
    d.update_latest(n)
    assert (Path(d.root) / "latest").resolve() == (Path(d.snaps) / n).resolve()


def test_remove_requires_approval(tmp_path, fake_ssh, approve):
    d = mk(tmp_path)
    n = d.reserve_snapshot(SNAP)
    with pytest.raises(ApprovalError):
        d.remove(n, None)
    with pytest.raises(ApprovalError):
        d.remove(n, approve("2026-02-02_100000"))
    assert (Path(d.snaps) / n).is_dir()
    d.remove(n, approve(n))
    assert not (Path(d.snaps) / n).exists()


@pytest.mark.parametrize("bad", ["..", "../x", "latest", "a b", ""])
def test_remove_rejects_non_snapshot_names(tmp_path, fake_ssh, approve, bad):
    d = mk(tmp_path)
    with pytest.raises(ValueError):
        d.remove(bad, approve(bad))


# ---- rsync por ssh falso (rsync real) ------------------------------------------------------------


@pytest.mark.needs_rsync
def test_push_meta_and_kit_through_rsync(tmp_path, fake_ssh):
    d = mk(tmp_path)
    n = d.reserve_snapshot(SNAP)
    stage = tmp_path / "stage"
    (stage / "_system_state").mkdir(parents=True)
    (stage / "_system_state" / "os.json").write_text("{}")
    (stage / "restore.sh").write_text("#!/bin/sh\n")
    d.push_meta(stage, n)
    assert (Path(d.snaps) / n / "_system_state" / "os.json").exists()
    assert (Path(d.snaps) / n / "restore.sh").stat().st_mode & 0o111
    d.write_kit_files({"disaster-recovery.sh": b"#!/bin/sh\n"})
    assert (Path(d.root) / "disaster-recovery.sh").stat().st_mode & 0o111


# ---- reachable / ready / presupuesto -----------------------------------------------------------------


def test_reachable_and_ready(tmp_path, fake_ssh):
    d = mk(tmp_path)
    ok, why = d.reachable()
    assert ok and "LAN" in why
    assert d.ensure_ready() == (True, "writable")


def test_unreachable_reports_the_reason(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "1")
    ok, why = mk(tmp_path).reachable()
    assert not ok and "failed" in why


def test_budget_uses_du_and_respects_floor(tmp_path, fake_ssh):
    d = mk(tmp_path, remote_max_gb=1)
    d.reserve_snapshot(SNAP)
    with mock.patch.object(d, "_tree_bytes", return_value=5_000_000_000):
        assert d.should_prune_more(5, 2) is True
        assert d.should_prune_more(2, 2) is False
        assert d.should_prune_more(1, 0) is False
    with mock.patch.object(d, "_tree_bytes", return_value=10):
        assert d.should_prune_more(5, 2) is False
    with mock.patch.object(d, "_tree_bytes", return_value=None):
        assert d.should_prune_more(5, 2) is False
    assert "snapshot tree" in d.usage_summary()


def test_budget_off_never_prunes(tmp_path):
    assert mk(tmp_path, remote_max_gb=0).should_prune_more(99, 2) is False


def test_real_du_runs_through_the_fake_ssh(tmp_path, fake_ssh):
    d = mk(tmp_path)
    d.reserve_snapshot(SNAP)
    assert isinstance(d._tree_bytes(), int)
