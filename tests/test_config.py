"""config/: esquema unión, entorno > archivo > defecto, claves heredadas, guardado en el lugar."""

import os
import re
import stat
from pathlib import Path

import pytest

from restore_fusion import config as cfgmod
from restore_fusion.config import Config, load, resolve_target, save_user_settings
from restore_fusion.config.schema import Config as RawConfig

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".config" / "restore-tui").mkdir(parents=True)
    return h


def conf(home):
    return home / ".config" / "restore-tui" / "config.conf"


def do_load(home, env=None, etc=None):
    return load("tester", environ=env or {}, home=home, etc_dir=etc or home / "etc")


# ---- defaults: nada de hosts/IPs/rutas -----------------------------------


def test_defaults_ship_no_host_ip_or_path(home):
    c = do_load(home)
    assert c.remote_host == "" and c.remote_host_lan == "" and c.remote_path == ""
    assert c.remote_name == "" and c.target_dir is None
    assert c.remote_configured is False
    assert c.remote_root("rsync") is None and c.remote_root("btrfs") is None
    assert c.pbs_repository == c.pbs_password == c.pbs_fingerprint == ""
    assert (c.max_disk_percent, c.min_keep, c.max_snapshots) == (80, 2, 0)
    assert c.engine == "auto" and c.remote_port == 22


def test_raw_dataclass_defaults_are_empty():
    c = RawConfig()
    assert not any([c.remote_host, c.remote_host_lan, c.remote_path, c.target_dir])


IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def test_no_ip_literals_in_code_or_example():
    files = list((ROOT / "restore_fusion").rglob("*.py")) + [ROOT / "config.conf.example"]
    for f in files:
        assert not IPV4.search(f.read_text()), f


def test_example_has_no_active_remote_values():
    active = [
        ln
        for ln in (ROOT / "config.conf.example").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    for ln in active:
        assert not ln.startswith(("REMOTE_", "PBS_", "TARGET_DIR")), ln


def test_password_not_in_repr(home):
    conf(home).write_text("PBS_PASSWORD=s3cret\n")
    assert "s3cret" not in repr(do_load(home))


# ---- orden env > archivo > defecto --------------------------------------


def test_file_is_read_and_never_rewritten(home):
    p = conf(home)
    p.write_text("# hi\nMIN_KEEP=5\nREMOTE_HOST=h.example\nEXCLUDE=a\nEXCLUDE=b\n")
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    c = do_load(home)
    assert c.min_keep == 5 and c.remote_host == "h.example"
    assert c.extra_exclusions == ["a", "b"] and c.config_source == str(p)
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime


def test_load_with_no_file_writes_nothing(home, tmp_path):
    do_load(home)
    assert not conf(home).exists()


def test_env_overrides_file(home):
    conf(home).write_text("MIN_KEEP=5\nTARGET_DIR=/from/file\n")
    c = do_load(home, {"RESTORE_TUI_MIN_KEEP": "7", "RESTORE_TUI_TARGET_DIR": "/from/env"})
    assert c.min_keep == 7 and c.target_dir == "/from/env"


def test_env_target_alias(home):
    assert do_load(home, {"RESTORE_TUI_TARGET": "/x"}).target_dir == "/x"


def test_legacy_env_prefix_still_works(home):
    c = do_load(home, {"BTRFS_REMOTE_HOST": "legacy.example", "BTRFS_MIN_KEEP": "4"})
    assert c.remote_host == "legacy.example" and c.min_keep == 4


def test_legacy_file_keys_still_work(home):
    conf(home).write_text("BTRFS_REMOTE_HOST=h.example\nBTRFS_REMOTE_PORT=2222\n")
    c = do_load(home)
    assert c.remote_host == "h.example" and c.remote_port == 2222


def test_second_candidate_btrfs_dir_is_read(home):
    d = home / ".config" / "btrfs-restore"
    d.mkdir(parents=True)
    (d / "config.conf").write_text("MIN_KEEP=9\n")
    assert do_load(home).min_keep == 9


def test_restore_tui_file_wins_over_btrfs_file(home):
    d = home / ".config" / "btrfs-restore"
    d.mkdir(parents=True)
    (d / "config.conf").write_text("MIN_KEEP=9\n")
    conf(home).write_text("MIN_KEEP=3\n")
    assert do_load(home).min_keep == 3


def test_etc_fallback(home):
    etc = home / "etc"
    (etc / "restore-tui").mkdir(parents=True)
    (etc / "restore-tui" / "config.conf").write_text("MIN_KEEP=6\n")
    assert do_load(home, etc=etc).min_keep == 6


def test_clamps_and_bad_numbers(home):
    c = do_load(home, {"RESTORE_TUI_MAX_DISK_PERCENT": "500", "RESTORE_TUI_MIN_KEEP": "0"})
    assert c.max_disk_percent == 99 and c.min_keep == 1
    assert do_load(home, {"RESTORE_TUI_MAX_DISK_PERCENT": "1"}).max_disk_percent == 10
    assert do_load(home, {"RESTORE_TUI_MIN_KEEP": "abc"}).min_keep == 2
    assert do_load(home, {"RESTORE_TUI_REMOTE_PORT": "x"}).remote_port == 22


def test_keep_aliases(home):
    conf(home).write_text("KEEP_SNAPSHOTS=4\n")
    assert do_load(home).max_snapshots == 4
    assert do_load(home, {"RESTORE_TUI_KEEP": "8"}).max_snapshots == 8


def test_exclusions_env_and_defaults_off(home):
    conf(home).write_text("EXCLUDE=a\nEXCLUDE_DEFAULTS=off\n")
    c = do_load(home, {"RESTORE_TUI_EXCLUDE": "b:c"})
    assert c.extra_exclusions == ["a", "b", "c"] and c.exclude_defaults is False


def test_remote_disabled_and_roots(home):
    conf(home).write_text("REMOTE_HOST=h.example\nREMOTE_PATH=/r/\nREMOTE_ENABLED=false\n")
    assert do_load(home).remote_root("rsync") is None
    conf(home).write_text("REMOTE_HOST=h.example\nREMOTE_PATH=/r/\n")
    c = do_load(home)
    assert c.remote_root("rsync") == "/r/restore-tui" and c.remote_root("btrfs") == "/r"
    assert c.remote_name == "h.example" and c.remote_user == "tester"


def test_engine_setting_is_lowercased_and_not_silently_fixed(home):
    assert do_load(home, {"RESTORE_TUI_ENGINE": "BTRFS"}).engine == "btrfs"
    assert do_load(home, {"RESTORE_TUI_ENGINE": "zfs"}).engine == "zfs"  # detect lo rechaza


# ---- resolve_target: regla 1 ---------------------------------------------


def test_resolve_target_explicit_unmounted_is_not_ok(home, tmp_path):
    c = Config(user="tester", target_dir=str(tmp_path))
    r = resolve_target(c, "btrfs", is_mount=lambda p: False)
    assert not r.ok and r.source == "config" and "mount point" in r.reason


def test_resolve_target_without_config_and_no_drive(home, tmp_path):
    media = tmp_path / "media"
    (media / "RANDOM").mkdir(parents=True)
    r = resolve_target(Config(user="u"), "rsync", bases=[media], is_mount=lambda p: True)
    assert not r.ok and r.source == "none" and r.mount is None


def test_resolve_target_autodetect_requires_mount_too(home, tmp_path):
    media = tmp_path / "media"
    (media / "USB_DATA").mkdir(parents=True)
    r = resolve_target(Config(user="u"), "rsync", bases=[media], is_mount=lambda p: False)
    assert not r.ok and r.source == "none"


def test_resolve_target_ok(home, tmp_path):
    media = tmp_path / "media"
    (media / "USB_DATA").mkdir(parents=True)
    r = resolve_target(
        Config(user="u"), "rsync", bases=[media], is_mount=lambda p: True, fstype=lambda p: "ext4"
    )
    assert r.ok and r.source == "autodetect" and r.root == media / "USB_DATA" / "restore-tui"


# ---- guardado en el lugar --------------------------------------------------


def test_save_creates_file_mode_600(home):
    shutil_home = home / "fresh"
    p = save_user_settings({"TARGET_DIR": "/x", "MIN_KEEP": "3"}, user="u", home=shutil_home)
    assert p == shutil_home / ".config" / "restore-tui" / "config.conf"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "TARGET_DIR=/x" in p.read_text()


def test_save_preserves_untouched_lines_and_comments(home):
    conf(home).write_text("# keep me\nMIN_KEEP=5\nEXCLUDE=*/a\nPBS_PASSWORD=zzz\n")
    save_user_settings({"MIN_KEEP": "9", "REMOTE_PORT": "2222"}, user="u", home=home)
    text = conf(home).read_text()
    assert "# keep me" in text and "EXCLUDE=*/a" in text and "PBS_PASSWORD=zzz" in text
    assert "MIN_KEEP=9" in text and "MIN_KEEP=5" not in text and "REMOTE_PORT=2222" in text


def test_save_round_trips_through_load(home):
    save_user_settings(
        {"TARGET_DIR": "/mnt/x", "REMOTE_HOST": "h.example", "ENGINE": "rsync"}, user="u", home=home
    )
    c = do_load(home)
    assert (c.target_dir, c.remote_host, c.engine) == ("/mnt/x", "h.example", "rsync")


def test_save_fixes_loose_permissions(home):
    conf(home).write_text("MIN_KEEP=2\n")
    os.chmod(conf(home), 0o644)
    save_user_settings({"MIN_KEEP": "3"}, user="u", home=home)
    assert stat.S_IMODE(conf(home).stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "key,val", [("bad key", "x"), ("A=B", "x"), ("MIN_KEEP", "1\nREMOTE_HOST=evil")]
)
def test_save_rejects_injection(home, key, val):
    with pytest.raises(ValueError):
        save_user_settings({key: val}, user="u", home=home)
    assert not conf(home).exists()


def test_save_leaves_no_temp_files(home):
    save_user_settings({"MIN_KEEP": "3"}, user="u", home=home)
    assert [p.name for p in conf(home).parent.iterdir()] == ["config.conf"]


def test_package_exports():
    assert cfgmod.Config is Config


# ---- REMOTE_KEEP (fase 1) -------------------------------------------------------------


def test_remote_keep_defaults_to_local_keep(home):
    cfg = do_load(home)
    assert cfg.remote_keep is None and cfg.effective_remote_keep == cfg.local_keep
    cfg.local_keep = 5
    assert cfg.effective_remote_keep == 5


def test_remote_keep_inherits_explicit_local_keep_from_file(home):
    conf(home).write_text("LOCAL_KEEP=4\n")
    assert do_load(home).effective_remote_keep == 4


def test_explicit_remote_keep_wins(home):
    conf(home).write_text("LOCAL_KEEP=4\nREMOTE_KEEP=7\n")
    cfg = do_load(home)
    assert cfg.local_keep == 4 and cfg.effective_remote_keep == 7


def test_remote_keep_minimum_is_one(home):
    conf(home).write_text("LOCAL_KEEP=4\nREMOTE_KEEP=0\n")
    # igual que LOCAL_KEEP: el loader sube a 1; RemoteBtrfs lo sube a 2 después
    assert do_load(home).effective_remote_keep == 1


def test_pbs_power_saver_defaults_on_and_turns_off(home):
    assert do_load(home).pbs_power_saver is True
    conf(home).write_text("PBS_POWER_SAVER=false\n")
    assert do_load(home).pbs_power_saver is False
