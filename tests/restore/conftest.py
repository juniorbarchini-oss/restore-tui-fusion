import pytest

from restore_fusion.restore.engine import RestoreEngine


@pytest.fixture
def tree(tmp_path):
    snap = tmp_path / "snap"
    dest = tmp_path / "dest"
    snap.mkdir()
    dest.mkdir()
    for n in ("a.txt", "b.txt", "c.txt"):
        (snap / n).write_text(n)
    return snap, dest


@pytest.fixture
def engine():
    return RestoreEngine(user="alice", target_uid=4242, target_gid=4242)
