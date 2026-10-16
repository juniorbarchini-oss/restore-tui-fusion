"""Regla 3 del README (restore honesto) y dueños. Portado de btrfs-restore-tui
(tests/test_restore_safety.py) a pytest, más los arreglos del plan (R5, R6)."""

import errno
import io
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.model.restore import ConflictResolution
from restore_fusion.restore import engine as eng

OVERWRITE = ConflictResolution.OVERWRITE
BACKUP = ConflictResolution.BACKUP
SKIP = ConflictResolution.SKIP


def items(engine, snap, *names):
    return engine.prepare_items([snap / n for n in names], snap)


# ---- errores por archivo ----------------------------------------------------


def test_one_failing_file_does_not_abort_and_final_state_is_not_success(tree, engine):
    snap, dest = tree
    real_copy = shutil.copy2

    def flaky(src, dst, *a, **k):
        if Path(src).name == "b.txt":
            raise PermissionError(13, "Permission denied")
        return real_copy(src, dst, *a, **k)

    with mock.patch("restore_fusion.restore.engine.shutil.copy2", side_effect=flaky):
        states = list(
            engine.restore_generator(
                items(engine, snap, "a.txt", "b.txt", "c.txt"), dest, OVERWRITE
            )
        )
    final = states[-1]
    assert final.done and final.error is not None  # NO es un éxito pelado
    assert final.failed_files == 1 and "b.txt" in final.error and not final.fatal
    assert (dest / "a.txt").exists() and (dest / "c.txt").exists()
    assert not (dest / "b.txt").exists()
    assert final.processed_files == 2
    assert final.ok is False


def test_clean_run_is_ok(tree, engine):
    snap, dest = tree
    final = list(engine.restore_generator(items(engine, snap, "a.txt"), dest, OVERWRITE))[-1]
    assert final.ok and final.current_file == "Completed" and final.percent == 100


def test_fatal_when_target_not_writable(tree, engine):
    snap, _ = tree
    bad = Path("/proc/nonexistent-dir/xyz")
    states = list(engine.restore_generator(items(engine, snap, "a.txt"), bad, OVERWRITE))
    assert len(states) == 1 and states[0].done and states[0].fatal
    assert "Cannot write" in states[0].error


def test_disk_full_aborts(tree, engine):
    snap, dest = tree

    def enospc(src, dst, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    with mock.patch("restore_fusion.restore.engine.shutil.copy2", side_effect=enospc):
        states = list(
            engine.restore_generator(
                items(engine, snap, "a.txt", "b.txt", "c.txt"), dest, OVERWRITE
            )
        )
    final = states[-1]
    assert final.done and final.fatal and "Disk full" in final.error
    assert final.processed_files == 0


# ---- R6: rsync 23 sin stderr ---------------------------------------------------


class FakeProc:
    def __init__(self, rc, stdout="", stderr=""):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = rc
        self.pid = 1

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def run_dir_with_fake_rsync(engine, tmp_path, rc, stderr=""):
    snap, dest = tmp_path / "s", tmp_path / "d"
    (snap / "proj").mkdir(parents=True)
    (snap / "proj" / "f").write_text("x")
    dest.mkdir()
    its = engine.prepare_items([snap / "proj"], snap)
    with (
        mock.patch("restore_fusion.restore.engine.shutil.which", return_value="/usr/bin/rsync"),
        mock.patch(
            "restore_fusion.restore.engine.subprocess.Popen",
            return_value=FakeProc(rc, stderr=stderr),
        ),
    ):
        return list(engine.restore_generator(its, dest, OVERWRITE))[-1]


def test_rsync_exit_23_with_empty_stderr_is_restored_with_errors(tmp_path, engine):
    final = run_dir_with_fake_rsync(engine, tmp_path, 23, "")
    assert final.done and final.error and final.failed_files >= 1
    assert "exit 23" in final.error
    assert final.current_file == "Completed with errors" and not final.ok


def test_rsync_exit_23_with_stderr_keeps_the_details(tmp_path, engine):
    final = run_dir_with_fake_rsync(
        engine, tmp_path, 23, "rsync: mkstemp failed: Permission denied (13)"
    )
    assert "Permission denied" in final.error


@pytest.mark.parametrize("rc", [1, 2, 12, 30])
def test_other_rsync_failures_are_errors(tmp_path, engine, rc):
    final = run_dir_with_fake_rsync(engine, tmp_path, rc, "")
    assert final.error and f"exited {rc}" in final.error


def test_rsync_exit_24_is_success(tmp_path, engine):
    final = run_dir_with_fake_rsync(engine, tmp_path, 24, "")
    assert final.ok


def test_rsync_enospc_is_fatal(tmp_path, engine):
    final = run_dir_with_fake_rsync(engine, tmp_path, 11, "")
    assert final.fatal and "Disk full" in final.error


# ---- directorios vacíos ---------------------------------------------------------


def test_empty_subdirs_are_recreated(tree, engine):
    snap, dest = tree
    t = snap / "project"
    (t / "src").mkdir(parents=True)
    (t / "src" / "main.py").write_text("x")
    (t / "empty").mkdir()
    (t / "nested" / "deeper").mkdir(parents=True)
    (t / "logs").mkdir()
    states = list(engine.restore_generator(engine.prepare_items([t], snap), dest, OVERWRITE))
    assert states[-1].done and not states[-1].error
    out = dest / "project"
    assert (out / "src" / "main.py").exists() and (out / "empty").is_dir()
    assert (out / "nested" / "deeper").is_dir() and (out / "logs").is_dir()


def test_selecting_a_single_empty_dir_restores_it(tree, engine):
    snap, dest = tree
    (snap / "onlydir").mkdir()
    list(engine.restore_generator(engine.prepare_items([snap / "onlydir"], snap), dest, OVERWRITE))
    assert (dest / "onlydir").is_dir()


# ---- dueños ------------------------------------------------------------------------


def test_within_user_home_detection(engine):
    assert engine._within_user_home(Path("/home/alice"))
    assert engine._within_user_home(Path("/home/alice/Documents/x"))
    assert not engine._within_user_home(Path("/"))
    assert not engine._within_user_home(Path("/etc"))
    assert not engine._within_user_home(Path("/home/bob"))


def test_system_restore_preserves_source_owner(tree, engine):
    snap, dest = tree
    seen = []
    fake_stat = os.stat_result((0o100644, 0, 0, 1, 0, 0, 5, 0, 0, 0))
    real_stat = Path.stat

    def stat_fn(self, *a, **k):
        # el origen "es de root (uid 0)"; el resto del filesystem es real
        if snap in (self, *self.parents):
            return fake_stat
        return real_stat(self, *a, **k)

    with (
        mock.patch(
            "restore_fusion.restore.engine.os.chown",
            side_effect=lambda p, u, g, *a, **k: seen.append((str(p), u, g)),
        ),
        mock.patch.object(Path, "stat", autospec=True, side_effect=stat_fn),
    ):
        list(
            engine.restore_generator(
                items(engine, snap, "a.txt"), dest, OVERWRITE, preserve_system_ownership=True
            )
        )
    mine = [c for c in seen if c[0].endswith("dest/a.txt")]
    assert mine and all(c[1] == 0 and c[2] == 0 for c in mine)


def test_home_restore_chowns_to_user(tree, engine):
    snap, dest = tree
    seen = []
    with mock.patch(
        "restore_fusion.restore.engine.os.chown",
        side_effect=lambda p, u, g, *a, **k: seen.append((str(p), u, g)),
    ):
        list(
            engine.restore_generator(
                items(engine, snap, "a.txt"), dest, OVERWRITE, preserve_system_ownership=False
            )
        )
    mine = [c for c in seen if c[0].endswith("dest/a.txt")]
    assert mine and all(c[1] == 4242 and c[2] == 4242 for c in mine)


# ---- R5: nunca se borra un .bak anterior -------------------------------------------------


def test_backup_never_deletes_an_older_bak_for_files(tree, engine):
    snap, dest = tree
    (dest / "a.txt").write_text("CURRENT")
    (dest / "a.txt.bak").write_text("OLDER BACKUP")
    list(engine.restore_generator(items(engine, snap, "a.txt"), dest, BACKUP))
    assert (dest / "a.txt").read_text() == "a.txt"
    assert (dest / "a.txt.bak").read_text() == "OLDER BACKUP"
    assert (dest / "a.txt.bak.1").read_text() == "CURRENT"


def test_backup_chain_keeps_every_generation(tree, engine):
    snap, dest = tree
    for gen in ("g1", "g2", "g3"):
        (dest / "a.txt").write_text(gen)
        list(engine.restore_generator(items(engine, snap, "a.txt"), dest, BACKUP))
    got = {p.name: p.read_text() for p in dest.glob("a.txt*")}
    assert got == {"a.txt": "a.txt", "a.txt.bak": "g1", "a.txt.bak.1": "g2", "a.txt.bak.2": "g3"}


def test_get_backup_path_skips_dangling_symlink(tree, engine):
    snap, dest = tree
    (dest / "x.bak").symlink_to("nowhere")
    assert engine._get_backup_path(dest / "x").name == "x.bak.1"


def test_overwrite_never_removes_a_directory_for_a_file(tree, engine):
    snap, dest = tree
    (dest / "a.txt").mkdir()
    (dest / "a.txt" / "precious").write_text("keep")
    final = list(engine.restore_generator(items(engine, snap, "a.txt"), dest, OVERWRITE))[-1]
    assert final.error and "directory" in final.error and final.failed_files == 1
    assert (dest / "a.txt" / "precious").read_text() == "keep"


def test_backup_moves_a_directory_aside_instead_of_deleting(tree, engine):
    snap, dest = tree
    (dest / "a.txt").mkdir()
    (dest / "a.txt" / "precious").write_text("keep")
    final = list(engine.restore_generator(items(engine, snap, "a.txt"), dest, BACKUP))[-1]
    assert final.ok and (dest / "a.txt").read_text() == "a.txt"
    assert (dest / "a.txt.bak" / "precious").read_text() == "keep"


def test_skip_keeps_existing_and_counts_it(tree, engine):
    snap, dest = tree
    (dest / "a.txt").write_text("LOCAL")
    final = list(engine.restore_generator(items(engine, snap, "a.txt"), dest, SKIP))[-1]
    assert (dest / "a.txt").read_text() == "LOCAL" and final.ok
