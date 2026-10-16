"""RestoreEngine: portado de btrfs-restore-tui (tests/test_engine.py) a pytest."""

import shutil
from pathlib import Path
from unittest import mock

import pytest

from restore_fusion.model.restore import ConflictResolution
from restore_fusion.restore.engine import RestoreEngine

OVERWRITE, BACKUP, SKIP = (
    ConflictResolution.OVERWRITE,
    ConflictResolution.BACKUP,
    ConflictResolution.SKIP,
)


@pytest.fixture
def snapdest(tmp_path):
    snap, dest = tmp_path / "snapshot_source", tmp_path / "restore_target"
    (snap / "subfolder").mkdir(parents=True)
    dest.mkdir()
    (snap / "test1.txt").write_text("snapshot file 1 content")
    (snap / "subfolder" / "test2.txt").write_text("snapshot file 2 nested content")
    return snap, dest


@pytest.fixture
def engine():
    return RestoreEngine(user="alice", target_uid=1, target_gid=1)


def test_prepare_items(snapdest, engine):
    snap, _ = snapdest
    items = engine.prepare_items([snap / "test1.txt", snap / "subfolder"], snap)
    assert len(items) == 2
    f = next(i for i in items if not i.is_dir)
    d = next(i for i in items if i.is_dir)
    assert f.rel_path == Path("test1.txt") and d.rel_path == Path("subfolder") and d.size_bytes > 0


def test_prepare_items_skips_missing(snapdest, engine):
    snap, _ = snapdest
    assert engine.prepare_items([snap / "nope"], snap) == []


def test_restore_clean(snapdest, engine):
    snap, dest = snapdest
    items = engine.prepare_items([snap / "test1.txt", snap / "subfolder"], snap)
    states = list(engine.restore_generator(items, dest, OVERWRITE))
    assert states[-1].done
    assert (dest / "test1.txt").read_text() == "snapshot file 1 content"
    assert (dest / "subfolder" / "test2.txt").exists()


def test_restore_with_backup_conflict(snapdest, engine):
    snap, dest = snapdest
    target = dest / "test1.txt"
    target.write_text("current modified file")
    items = engine.prepare_items([snap / "test1.txt"], snap)
    assert list(engine.restore_generator(items, dest, BACKUP))[-1].done
    assert target.read_text() == "snapshot file 1 content"
    assert (dest / "test1.txt.bak").read_text() == "current modified file"


@pytest.fixture
def proj(tmp_path):
    snap, dest = tmp_path / "snap", tmp_path / "dest"
    tree = snap / "proj"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "main.py").write_text("print('hi')\n")
    (tree / "docs").mkdir()
    (tree / "docs" / "readme.md").write_text("# hi\n")
    (tree / "empty").mkdir()
    (tree / "src" / "link.py").symlink_to("main.py")
    dest.mkdir()
    return snap, dest


def run(engine, snap, dest, resolution=OVERWRITE):
    items = engine.prepare_items([snap / "proj"], snap)
    return list(engine.restore_generator(items, dest, resolution))


@pytest.mark.needs_rsync
def test_dir_tree_files_symlink_and_empty_dir_all_restored(proj, engine):
    snap, dest = proj
    states = run(engine, snap, dest)
    assert states[-1].done and not states[-1].error
    out = dest / "proj"
    assert (out / "src" / "main.py").read_text() == "print('hi')\n"
    assert (out / "docs" / "readme.md").read_text() == "# hi\n"
    assert (out / "empty").is_dir() and (out / "src" / "link.py").is_symlink()
    assert states[-1].processed_files == states[-1].total_files


@pytest.mark.needs_rsync
def test_skip_keeps_an_existing_file(proj, engine):
    snap, dest = proj
    (dest / "proj" / "src").mkdir(parents=True)
    (dest / "proj" / "src" / "main.py").write_text("LOCAL EDIT\n")
    run(engine, snap, dest, SKIP)
    assert (dest / "proj" / "src" / "main.py").read_text() == "LOCAL EDIT\n"


@pytest.mark.needs_rsync
def test_backup_moves_the_existing_file_aside(proj, engine):
    snap, dest = proj
    (dest / "proj" / "docs").mkdir(parents=True)
    (dest / "proj" / "docs" / "readme.md").write_text("OLD\n")
    final = run(engine, snap, dest, BACKUP)[-1]
    assert final.ok
    assert (dest / "proj" / "docs" / "readme.md").read_text() == "# hi\n"
    assert (dest / "proj" / "docs" / "readme.md.bak").read_text() == "OLD\n"


@pytest.mark.needs_rsync
def test_dir_backup_never_deletes_an_older_bak(proj, engine):
    snap, dest = proj
    d = dest / "proj" / "docs"
    d.mkdir(parents=True)
    (d / "readme.md").write_text("CURRENT\n")
    (d / "readme.md.bak").write_text("OLDER BACKUP\n")
    final = run(engine, snap, dest, BACKUP)[-1]
    assert final.ok
    assert (d / "readme.md").read_text() == "# hi\n"
    assert (d / "readme.md.bak").read_text() == "OLDER BACKUP\n"
    assert (d / "readme.md.bak.1").read_text() == "CURRENT\n"


@pytest.mark.needs_rsync
def test_dir_backup_leaves_identical_files_alone(proj, engine):
    snap, dest = proj
    run(engine, snap, dest, OVERWRITE)  # primera vez: copia todo
    final = run(engine, snap, dest, BACKUP)[-1]  # segunda: nada cambió
    assert final.ok
    assert not list(dest.rglob("*.bak*"))


@pytest.mark.needs_rsync
def test_dir_backup_aborts_item_if_the_plan_fails(proj, engine):
    snap, dest = proj
    (dest / "proj" / "docs").mkdir(parents=True)
    (dest / "proj" / "docs" / "readme.md").write_text("OLD\n")
    with mock.patch.object(engine, "_backup_aside", return_value=([], "could not plan")):
        final = run(engine, snap, dest, BACKUP)[-1]
    assert final.error and "could not plan" in final.error
    # nada se sobrescribió sin respaldo
    assert (dest / "proj" / "docs" / "readme.md").read_text() == "OLD\n"


def test_rsync_partial_failure_is_reported_not_fatal(proj, engine):
    snap, dest = proj

    def fake_rsync(src, dst, res, to_user):
        yield ("progress", 1, 10, "main.py")
        yield ("error", "rsync: mkstemp failed: Permission denied (13)")

    with (
        mock.patch("restore_fusion.restore.engine.shutil.which", return_value="/usr/bin/rsync"),
        mock.patch.object(engine, "_rsync_item", side_effect=fake_rsync),
    ):
        final = run(engine, snap, dest)[-1]
    assert final.done and not final.fatal and "Permission denied" in final.error


def test_falls_back_to_per_file_copy_without_rsync(proj, engine):
    snap, dest = proj
    with mock.patch("restore_fusion.restore.engine.shutil.which", return_value=None):
        states = run(engine, snap, dest)
    assert states[-1].done and not states[-1].error
    assert (dest / "proj" / "src" / "main.py").read_text() == "print('hi')\n"


def test_rsync_argv_is_a_list_and_never_uses_backup_suffix(engine, tmp_path):
    argv = engine._rsync_argv(tmp_path / "a", tmp_path / "b", BACKUP, True)
    assert isinstance(argv, list) and argv[0] == "rsync"
    assert "--backup" not in argv and not any(a.startswith("--suffix") for a in argv)
    assert argv[-3] == "--"
    assert "--ignore-existing" in engine._rsync_argv(tmp_path / "a", tmp_path / "b", SKIP, True)
    assert "-n" in engine._rsync_argv(tmp_path / "a", tmp_path / "b", BACKUP, True, dry_run=True)


def test_malicious_path_stays_one_argument(engine, tmp_path):
    evil = tmp_path / "a; rm -rf ~"
    argv = engine._rsync_argv(evil, tmp_path / "b", OVERWRITE, False)
    assert f"{evil}/" in argv


def test_nothing_is_preselected_by_the_engine(engine):
    # el motor no elige archivos por su cuenta: sin items no hace nada
    states = list(engine.restore_generator([], Path("/tmp"), OVERWRITE))
    assert states[-1].done and states[-1].total_files == 0


@pytest.mark.needs_rsync
def test_real_rsync_progress_lines_are_parsed(proj, engine):
    # rsync escapa los caracteres de control; con el separador "|" el progreso sí se parsea
    snap, dest = proj
    states = run(engine, snap, dest)
    assert any(s.processed_files > 0 and not s.done for s in states)
