"""`restore-tui --paths` / `--gc`: lista honesta y limpieza SOLO de corridas muertas."""

import io
import os
from pathlib import Path

from rich.console import Console

from restore_fusion import cli, maintenance
from restore_fusion.config.schema import Config


def cfg_for(tmp_path):
    return Config(user="tester", home=tmp_path / "home", local_snapshots_dir=tmp_path / "snaps")


def test_paths_report_names_the_install_config_and_state(tmp_path):
    rows = dict(maintenance.paths_report(cfg_for(tmp_path), tmp_path / "home"))
    keys = " ".join(rows)
    for needle in ("/opt/restore-fusion/", "restore-fusion.desktop", "config.conf", ".backup-tmp",
                   "staging", "restore-fusion-archive"):  # fmt: skip
        assert needle in keys
    assert "never touched" in rows[str(tmp_path / "home/.config/btrfs-restore/config.conf")]


def test_paths_has_no_ip_or_default_host(tmp_path):
    import re

    text = " ".join(f"{a} {b}" for a, b in maintenance.paths_report(cfg_for(tmp_path), tmp_path))
    assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", text)
    assert "<remote>" in text


def test_run_paths_prints_without_markup_errors(tmp_path):
    buf = io.StringIO()
    assert maintenance.run_paths(cfg_for(tmp_path), tmp_path, Console(file=buf, width=250)) == 0
    assert "{root,home}_<ts>" in buf.getvalue()  # llaves y <> literales, sin que rich los coma


def test_gc_sweeps_dead_runs_only(tmp_path):
    cfg = cfg_for(tmp_path)
    snaps = Path(cfg.local_snapshots_dir)
    dead_pid = 2**22 + 12345  # no existe
    tmp = snaps / ".backup-tmp"
    dead, live = tmp / f"{dead_pid}-20260101_000000", tmp / f"{os.getpid()}-20260101_000000"
    other = tmp / "keep-me"
    for d in (dead, live, other):
        d.mkdir(parents=True)
        (d / "f").write_text("x")
    snap = snaps / "root_20260910_000000"
    snap.mkdir()
    (snap / "data").write_text("backup")
    buf = io.StringIO()
    assert maintenance.run_gc(cfg, out=Console(file=buf)) == 0
    assert not dead.exists()
    assert live.exists() and other.exists(), "a live run and unknown dirs are never touched"
    assert (snap / "data").read_text() == "backup", "gc never touches snapshots"


def test_gc_with_nothing_to_do_says_so(tmp_path):
    buf = io.StringIO()
    maintenance.run_gc(cfg_for(tmp_path), out=Console(file=buf))
    assert "nothing to collect" in buf.getvalue()


def test_cli_routes_the_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_run_mode", lambda m, a=None: seen.append((m, a)) or 0)
    assert cli.main(["--gc"]) == 0 and cli.main(["--paths"]) == 0
    assert seen == [
        ("restore_fusion.maintenance", ["--gc"]),
        ("restore_fusion.maintenance", ["--paths"]),
    ]
