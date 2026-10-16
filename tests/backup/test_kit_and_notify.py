"""Kit de recuperación (4 plantillas) y alertas. Portado de restore-tui (test_recovery_kit.py) y
btrfs-restore-tui (test_error_capture_and_alert.py, test_backup.py: kit remoto)."""

import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.backup import kit as kitmod
from restore_fusion.backup import notify
from restore_fusion.destinations import LocalDestination, SSHDestination

COMBOS = [("rsync", False), ("rsync", True), ("btrfs", False), ("btrfs", True)]


def dest_for(remote: bool, tmp_path):
    if remote:
        return SSHDestination(
            host="h.example",
            root="/srv/my backups",
            user="bob",
            port=2222,
            label="R",
            tcp_check=lambda h, p: True,
        )
    return LocalDestination(tmp_path / "usb" / "x", is_mount=lambda p: True)


@pytest.mark.parametrize("engine,remote", COMBOS)
def test_kit_scripts_are_valid_bash_with_placeholders_filled(tmp_path, engine, remote):
    files = kitmod.kit_files(
        engine, dest_for(remote, tmp_path), latest="2026-01-01_100000", host="host1", tarball=b"T"
    )
    assert set(files) == {"disaster-recovery.sh", "RECOVERY.md", "restore-fusion-src.tar.gz"}
    script, md = files["disaster-recovery.sh"].decode(), files["RECOVERY.md"].decode()
    assert "@@" not in script and "@@" not in md and "{host}" not in md
    assert script.startswith("#!/usr/bin/env bash\n")
    rc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert rc.returncode == 0, rc.stderr
    assert "host1" in md
    if remote:
        assert 'HOST="bob@h.example"' in script and 'BASE="/srv/my backups"' in script
        assert 'PORT="2222"' in script and "StrictHostKeyChecking=accept-new" in script
    else:
        assert 'SNAPS="${HERE}/snapshots"' in script and "2026-01-01_100000" in md


@pytest.mark.parametrize("engine,remote", COMBOS)
def test_shellcheck_error_level(tmp_path, engine, remote):
    import shutil

    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    p = tmp_path / "dr.sh"
    p.write_bytes(
        kitmod.kit_files(engine, dest_for(remote, tmp_path), latest="n", host="h", tarball=b"")[
            "disaster-recovery.sh"
        ]
    )
    assert (
        subprocess.run(["shellcheck", "-S", "error", str(p)], capture_output=True).returncode == 0
    )


def test_kit_has_no_tarball_when_it_cannot_be_built(tmp_path):
    files = kitmod.kit_files("rsync", dest_for(False, tmp_path), latest="n", host="h", tarball=b"")
    assert "restore-fusion-src.tar.gz" not in files


def test_src_tarball_holds_the_app_and_its_installer():
    data = kitmod.build_src_tarball()
    assert data
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as f:
        f.write(data)
        f.flush()
        names = tarfile.open(f.name).getnames()
    assert any(n.startswith("restore_fusion/") for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(n.startswith(("sources", "tests", ".git")) for n in names)
    assert "bin/restore-tui" in names


def test_src_tarball_empty_when_nothing_to_pack(tmp_path):
    assert kitmod.build_src_tarball(tmp_path) == b""


def test_remote_picker_only_lists_date_named_directories():
    """Hallazgo real: el meta/ remoto también tiene archivos sueltos de scripts viejos; el selector
    del script remoto btrfs solo debe listar directorios con nombre de fecha."""
    script = (kitmod.TEMPLATES / "btrfs_remote.sh").read_text()
    assert "-type d" in script
    assert r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}" in script


def test_old_names_are_not_left_in_the_md_files():
    for f in kitmod.TEMPLATES.glob("*.md"):
        t = f.read_text(encoding="utf-8")
        assert (
            "btrfs-restore-tui" not in t
            and "restore-tui-src" not in t
            and "made by restore-tui" not in t
        ), f


def test_the_remote_md_is_not_mojibake():
    t = (kitmod.TEMPLATES / "btrfs_remote.md").read_text(encoding="utf-8")
    assert "â" not in t and "—" in t


# ---- alertas (port) -------------------------------------------------------------------------


@pytest.fixture
def alert(tmp_path):
    state = tmp_path / ".last-alert"

    def send(**kw):
        run = kw.pop("run", mock.Mock(return_value=mock.Mock(returncode=0)))
        ok = notify.send_alert("T", "body", "root", state, run=run, **kw)
        return ok, run, state

    return send


def test_root_delivers_to_the_users_session_bus(alert):
    ok, run, _ = alert(is_root=True, exists=lambda p: True)
    assert ok
    cmd = run.call_args.args[0]
    assert cmd[:4] == ["runuser", "-u", "root", "--"] and "notify-send" in cmd
    assert any(c.startswith("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/") for c in cmd)


def test_no_graphical_session_means_no_alert_and_no_crash(alert):
    ok, run, _ = alert(is_root=True, exists=lambda p: False)
    assert not ok
    run.assert_not_called()


def test_same_alert_is_not_repeated_within_the_window(alert):
    assert alert(is_root=False, now=lambda: 1000.0)[0]
    assert not alert(is_root=False, now=lambda: 1060.0)[0]
    assert alert(is_root=False, now=lambda: 1000.0 + notify.REPEAT + 1)[0]


def test_clear_alert_allows_the_next_one(alert):
    _ok, _run, state = alert(is_root=False, now=lambda: 1000.0)
    notify.clear_alert(state)
    assert alert(is_root=False, now=lambda: 1001.0)[0]


def test_missing_notify_send_never_raises(alert):
    assert not alert(is_root=False, run=mock.Mock(side_effect=FileNotFoundError("notify-send")))[0]


def test_failed_notify_send_is_not_recorded_as_sent(alert):
    ok, _run, state = alert(is_root=False, run=mock.Mock(return_value=mock.Mock(returncode=1)))
    assert not ok and not state.exists()
