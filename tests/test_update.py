"""update.py: daily new-version check and the interactive [U] Update."""

from pathlib import Path
from unittest import mock

from rich.console import Console

from restore_fusion import update

A, B = "a" * 40, "b" * 40
T = 10**9  # a plausible "now" (the cache treats a missing file as time 0)


def installed(tmp_path, commit=A):
    root = tmp_path / "opt"
    root.mkdir()
    (root / ".owned-paths").write_text("/opt/restore-fusion\n")
    if commit:
        (root / "VERSION").write_text(commit + "\n")
    return root


def test_source_tree_is_not_checked(tmp_path):
    fetch = mock.Mock(return_value=B)
    assert update.available(tmp_path, root=tmp_path / "nope", fetch=fetch) is None
    fetch.assert_not_called()


def test_newer_commit_is_reported_and_cached(tmp_path):
    root, home = installed(tmp_path), tmp_path / "home"
    fetch = mock.Mock(return_value=B)
    assert update.available(home, root=root, fetch=fetch, now=T) == B
    assert update.available(home, root=root, fetch=fetch, now=T + 60) == B
    fetch.assert_called_once()  # the second call used the cache


def test_checks_again_after_a_day(tmp_path):
    root, home = installed(tmp_path), tmp_path / "home"
    fetch = mock.Mock(return_value=B)
    update.available(home, root=root, fetch=fetch, now=T)
    update.available(home, root=root, fetch=fetch, now=T + update.CHECK_EVERY + 1)
    assert fetch.call_count == 2


def test_same_commit_means_up_to_date(tmp_path):
    root = installed(tmp_path)
    assert update.available(tmp_path, root=root, fetch=lambda: A, now=T) is None


def test_offline_is_silent_and_not_retried_all_day(tmp_path):
    root, home = installed(tmp_path), tmp_path / "home"
    fetch = mock.Mock(return_value=None)
    assert update.available(home, root=root, fetch=fetch, now=T) is None
    assert update.available(home, root=root, fetch=fetch, now=T + 100) is None
    fetch.assert_called_once()


def test_disabled_or_unknown_version_never_fetches(tmp_path):
    fetch = mock.Mock(return_value=B)
    assert update.available(tmp_path, enabled=False, root=installed(tmp_path), fetch=fetch) is None
    (tmp_path / "x").mkdir()
    assert (
        update.available(tmp_path, root=installed(tmp_path / "x", commit=None), fetch=fetch) is None
    )
    fetch.assert_not_called()


def con():
    c = Console(record=True, width=120)
    return c


def test_update_refuses_on_a_source_tree(tmp_path):
    assert update.run_update(con(), tmp_path, root=tmp_path / "nope") is False


def test_update_cancelled_runs_nothing(tmp_path):
    root, run = installed(tmp_path), mock.Mock()
    c = con()
    c.input = lambda *a, **k: "n"
    assert update.run_update(c, tmp_path, root=root, fetch=lambda: B, run=run) is False
    run.assert_not_called()


def test_update_up_to_date_runs_nothing(tmp_path):
    root, run = installed(tmp_path), mock.Mock()
    assert update.run_update(con(), tmp_path, root=root, fetch=lambda: A, run=run) is False
    run.assert_not_called()


def test_update_clones_runs_installer_and_cleans_up(tmp_path):
    root, home = installed(tmp_path), tmp_path / "home"
    calls, tmpdirs = [], []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv[0] == "git":
            Path(argv[-1]).mkdir(parents=True)
            tmpdirs.append(Path(argv[-1]).parent)
        return mock.Mock(returncode=0)

    c = con()
    c.input = lambda *a, **k: "y"
    assert update.run_update(c, home, root=root, fetch=lambda: B, run=fake_run) is True
    assert calls[0][0][:2] == ["git", "clone"]
    assert calls[1][0] == ["./install.sh", "--upgrade", "--yes"]
    assert calls[1][1]["cwd"] == tmpdirs[0] / "src"
    assert not tmpdirs[0].exists()
    assert update.cache_file(home).read_text().split()[1] == B


def test_failed_installer_reports_and_still_cleans_up(tmp_path):
    root, tmpdirs = installed(tmp_path), []

    def fake_run(argv, **kw):
        if argv[0] == "git":
            Path(argv[-1]).mkdir(parents=True)
            tmpdirs.append(Path(argv[-1]).parent)
            return mock.Mock(returncode=0)
        return mock.Mock(returncode=1)

    c = con()
    c.input = lambda *a, **k: "y"
    assert update.run_update(c, tmp_path, root=root, fetch=lambda: B, run=fake_run) is False
    assert not tmpdirs[0].exists()
    assert not update.cache_file(tmp_path).exists()
