"""BtrfsOps: comandos solo como argv (sin shell), progreso, cancelación, push_tree.
Portado de btrfs-restore-tui (tests/test_btrfs_ops.py) a pytest."""

import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.engines.btrfs import ops as btrfs_ops
from restore_fusion.engines.btrfs.ops import BtrfsOps, _human, _kill_process_group, prune_paths

# ---- argv ---------------------------------------------------------------------------


def test_send_argv_is_a_list_never_a_string():
    argv = BtrfsOps()._send_argv(Path("/mnt/x'; touch /tmp/pwned; '"), None)
    assert isinstance(argv, list) and argv[:2] == ["btrfs", "send"]
    assert argv[-1] == "/mnt/x'; touch /tmp/pwned; '"


def test_send_argv_with_parent():
    assert BtrfsOps()._send_argv(Path("/s/new"), Path("/s/old")) == [
        "btrfs", "send", "-p", "/s/old", "/s/new"]  # fmt: skip


def test_remote_commands_are_quoted_for_the_remote_shell(fake_ssh, tmp_path):
    """El comando remoto pasa por un shell: con shlex.join un nombre raro llega como UN argumento."""
    ops = BtrfsOps()
    odd = tmp_path / 're mote\'s "d" $HOME; `x`'
    odd.mkdir()
    marker = tmp_path / "pwned"
    res = ops.ssh_capture(["ssh"], "u@h", ["ls", "-1d", "--", str(odd)])
    assert res.returncode == 0 and res.stdout.strip() == str(odd)
    res = ops.ssh_capture(["ssh"], "u@h", ["echo", f"; touch {marker}"])
    assert res.stdout.strip() == f"; touch {marker}" and not marker.exists()


def test_push_tree_into_a_path_with_spaces_through_a_real_shell(fake_ssh, tmp_path):
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f").write_text("hi")
    dest = tmp_path / "re mote's dir"
    rc, err = BtrfsOps().push_tree(src, ["ssh"], "u@h", str(dest))
    assert (rc, err) == (0, "")
    assert (dest / "d" / "f").read_text() == "hi"


def test_send_ssh_receive_sink_is_one_quoted_remote_command():
    ops = BtrfsOps()
    seen = {}
    with mock.patch.object(
        ops, "_send_pipe", side_effect=lambda send, sink, **k: seen.update(sink=sink) or 0
    ):
        ops.send_ssh_receive(
            Path("/s/n"),
            None,
            ["ssh", "-p", "22"],
            "u@h",
            ["sudo", "btrfs", "receive", "/srv/my backups/root/"],
        )
    assert seen["sink"] == ["ssh", "-p", "22", "u@h", "sudo btrfs receive '/srv/my backups/root/'"]


# ---- contador de bytes -----------------------------------------------------------------


def test_run_counted_moves_bytes_and_reports():
    seen = []
    ops = BtrfsOps(progress_cb=seen.append)
    ops._use_pv, ops._count = False, True
    with open("/dev/null", "wb") as out:
        rc = ops._run_counted(["head", "-c", "3000000", "/dev/zero"], ["cat"], out)
    assert rc == 0 and seen and "sent" in seen[-1]


def test_pipe_failure_propagates():
    ops = BtrfsOps(progress_cb=lambda s: None)
    ops._use_pv, ops._count = False, True
    with open("/dev/null", "wb") as out:
        assert ops._run_counted(["false"], ["cat"], out) != 0


# ---- prune_paths ---------------------------------------------------------------------------


def test_removes_matches_keeps_the_rest_and_skips_subvolumes(tmp_path):
    root = tmp_path
    (root / "alice" / ".cache" / "x").mkdir(parents=True)
    (root / "alice" / ".cache" / "x" / "blob").write_text("junk")
    (root / "alice" / "Documents").mkdir(parents=True)
    (root / "alice" / "Documents" / "keep.txt").write_text("keep")
    (root / "var" / "log").mkdir(parents=True)  # subvolumen anidado
    (root / "var" / "tmp" / "j").mkdir(parents=True)
    removed = prune_paths(
        root, ["*/.cache", "var/tmp/*", "var/log"], is_subvolume=lambda p: p.name == "log"
    )
    assert "alice/.cache" in removed and "var/tmp/j" in removed and "var/log" not in removed
    assert not (root / "alice" / ".cache").exists() and (root / "var" / "log").exists()
    assert (root / "alice" / "Documents" / "keep.txt").exists()


def test_missing_glob_is_a_noop(tmp_path):
    assert prune_paths(tmp_path, ["nope/*", "*/.cache"]) == []


@pytest.mark.parametrize(
    "pat", ["../victim", "../../victim", "a/../../victim", "/etc/*", "", "x\x00y"]
)
def test_unsafe_patterns_are_ignored(tmp_path, pat):
    root = tmp_path / "snap"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f").write_text("keep")
    assert prune_paths(root, [pat]) == []
    assert (victim / "f").exists()


def test_symlink_matches_are_unlinked_not_followed(tmp_path):
    root = tmp_path / "snap"
    root.mkdir()
    out = tmp_path / "outside"
    out.mkdir()
    (out / "f").write_text("keep")
    (root / "link").symlink_to(out)
    assert prune_paths(root, ["link"]) == ["link"]
    assert not (root / "link").exists() and (out / "f").exists()
    (root / "link2").symlink_to(out)
    assert prune_paths(root, ["link2/*"]) == []  # no se entra por el enlace
    assert (out / "f").exists()


# ---- cancelación ---------------------------------------------------------------------------------


def test_kill_process_group_stops_detached_stages():
    procs = [subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(2)]
    _kill_process_group(procs, grace=1.0)
    assert all(p.poll() is not None for p in procs)


def test_run_pipeline_interrupt_leaves_no_orphans():
    ops = BtrfsOps()
    real_wait = subprocess.Popen.wait
    fired, killed = [], []

    def fake_wait(self, timeout=None):
        if timeout is None and not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    real_kill = btrfs_ops._kill_process_group

    def spy_kill(procs, **kw):
        killed.extend(procs)
        return real_kill(procs, **kw)

    with (
        mock.patch.object(subprocess.Popen, "wait", fake_wait),
        mock.patch.object(btrfs_ops, "_kill_process_group", spy_kill),
        open(os.devnull, "wb") as out,
    ):
        with pytest.raises(KeyboardInterrupt):
            ops.run_pipeline([["sleep", "30"], ["cat"]], final_stdout=out)
    assert killed
    time.sleep(0.2)
    assert all(p.poll() is not None for p in killed)


# ---- push_tree -------------------------------------------------------------------------------------


def test_stalled_receiver_times_out_with_124(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("hi")
    # "remoto" = sh -c <script>; el comando remoto llega como UNA cadena (shlex.join) en $0:
    # duerme solo en el paso de recepción (tar ...), no en el mkdir
    rc, err = BtrfsOps().push_tree(
        src,
        ["sh", "-c"],
        'case "$0" in "tar "*) exec sleep 5;; esac',
        str(tmp_path / "dest"),
        timeout=1,
    )
    assert rc == 124 and "timed out" in err


def test_empty_local_dir_is_an_error_not_a_silent_success(tmp_path):
    """Un tar vacío "extrae" bien en el otro lado; push_tree no debe reportar rc=0 si no había nada
    que enviar (hallazgo real: un state dir vaciado por otro llamador parecía un éxito mudo)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, err = BtrfsOps().push_tree(empty, ["true"], "remote", str(tmp_path / "dest"))
    assert rc != 0 and "empty" in err


def test_missing_local_dir_is_an_error(tmp_path):
    rc, err = BtrfsOps().push_tree(tmp_path / "nope", ["true"], "remote", str(tmp_path / "dest"))
    assert rc != 0 and "missing" in err


def test_human_units():
    assert _human(0) == "0.0 B" and _human(1536) == "1.5 KiB" and _human(5 * 1024**3) == "5.0 GiB"
