"""drives.py: regla 1 del README (sin fallback, punto de montaje real)."""

from pathlib import Path

import pytest

from restore_fusion import drives
from restore_fusion.drives import (
    autodetect_target,
    candidate_drives,
    holds_other_edition_layout,
    list_backup_drives,
    split_target,
    validate_target,
)


def all_mounts(_p):
    return True


def no_mounts(_p):
    return False


def only(*mounted):
    s = {Path(m) for m in mounted}
    return lambda p: Path(p) in s


@pytest.fixture
def media(tmp_path):
    m = tmp_path / "media"
    m.mkdir()
    return m


# ---- candidatos -----------------------------------------------------------


def test_stale_mountpoint_dir_is_not_a_candidate(media):
    # directorio escribible que quedó tras desenchufar: existe pero no es mount
    (media / "USB_DATA").mkdir()
    assert candidate_drives("u", "rsync", [media], no_mounts) == []
    assert autodetect_target("u", "rsync", bases=[media], is_mount=no_mounts) is None


def test_mounted_drive_is_a_candidate(media):
    (media / "USB_DATA").mkdir()
    assert candidate_drives("u", "rsync", [media], all_mounts) == [media / "USB_DATA"]


def test_mnt_is_never_a_default_base():
    assert Path("/mnt") not in drives.default_bases("someone")
    assert all("mnt" not in str(b) for b in drives.default_bases("someone"))


def test_no_fallback_with_two_unlabelled_drives(media):
    (media / "RANDOM_A").mkdir()
    (media / "RANDOM_B").mkdir()
    for engine in ("rsync", "btrfs"):
        assert autodetect_target("u", engine, bases=[media], is_mount=all_mounts) is None


def test_no_fallback_with_single_unlabelled_drive(media):
    (media / "RANDOM_STICK").mkdir()
    assert autodetect_target("u", "btrfs", bases=[media], is_mount=all_mounts) is None


def test_label_match_picks_the_labelled_drive(media):
    (media / "RANDOM").mkdir()
    (media / "USB_DATA").mkdir()
    got = autodetect_target("u", "rsync", bases=[media], is_mount=all_mounts)
    assert got == str(media / "USB_DATA")


def test_custom_label_is_honoured(media):
    (media / "MI_BACKUP").mkdir()
    got = autodetect_target("u", "rsync", label="MI_BACKUP", bases=[media], is_mount=all_mounts)
    assert got == str(media / "MI_BACKUP")


def test_existing_own_backup_wins_over_label(media):
    (media / "USB_DATA").mkdir()
    (media / "OLD" / "restore-tui" / "snapshots").mkdir(parents=True)
    got = autodetect_target("u", "rsync", label="NOPE", bases=[media], is_mount=all_mounts)
    assert got == str(media / "OLD")


def test_existing_own_backup_btrfs_engine_uses_its_own_folder(media):
    (media / "OLD" / "btrfs-restore" / "snapshots").mkdir(parents=True)
    (media / "OTHER" / "restore-tui" / "snapshots").mkdir(parents=True)
    got = autodetect_target("u", "btrfs", label="NOPE", bases=[media], is_mount=all_mounts)
    assert got == str(media / "OLD")


def test_unmounted_labelled_drive_is_not_chosen_even_if_another_is_mounted(media):
    (media / "USB_DATA").mkdir()
    (media / "OTHER").mkdir()
    got = autodetect_target("u", "rsync", bases=[media], is_mount=only(media / "OTHER"))
    assert got is None


# ---- layouts de la otra edición (los dos) ---------------------------------


def test_legacy_btrfs_layout_is_foreign_for_rsync(media):
    d = media / "USB_BTRFS"
    for n in ("home", "root", "meta"):
        (d / n).mkdir(parents=True)
    assert holds_other_edition_layout(d, "rsync")
    assert autodetect_target("u", "rsync", bases=[media], is_mount=all_mounts) is None


def test_current_btrfs_folder_is_foreign_for_rsync(media):
    d = media / "USB_DATA"  # aunque la etiqueta coincida
    (d / "btrfs-restore" / "snapshots").mkdir(parents=True)
    assert holds_other_edition_layout(d, "rsync")
    assert candidate_drives("u", "rsync", [media], all_mounts) == []


def test_rsync_folder_is_foreign_for_btrfs(media):
    d = media / "USB_DATA"
    (d / "restore-tui" / "snapshots").mkdir(parents=True)
    assert holds_other_edition_layout(d, "btrfs")


def test_drive_with_both_folders_is_not_foreign(media):
    d = media / "SHARED"
    (d / "restore-tui").mkdir(parents=True)
    (d / "btrfs-restore").mkdir()
    assert not holds_other_edition_layout(d, "rsync")
    assert not holds_other_edition_layout(d, "btrfs")


def test_list_backup_drives_reports_fstype(media):
    (media / "A").mkdir()
    got = list_backup_drives("u", "rsync", [media], all_mounts, fstype=lambda p: "ext4")
    assert got == [(media / "A", "ext4")]


# ---- validate_target ------------------------------------------------------


def test_explicit_unmounted_target_dir_rejected(tmp_path):
    ok, why = validate_target(str(tmp_path), "btrfs", is_mount=no_mounts)
    assert not ok and "not a mount point" in why


def test_explicit_unmounted_suffixed_target_dir_rejected(tmp_path):
    ok, why = validate_target(str(tmp_path / "restore-tui"), "rsync", is_mount=no_mounts)
    assert not ok


def test_missing_target_dir_rejected(tmp_path):
    ok, why = validate_target(str(tmp_path / "nope"), "btrfs", is_mount=all_mounts)
    assert not ok and "does not exist" in why


def test_empty_target_rejected():
    assert validate_target("", "rsync")[0] is False


def test_mounted_ext4_accepted_for_rsync(tmp_path):
    ok, _ = validate_target(str(tmp_path), "rsync", all_mounts, fstype=lambda p: "ext4")
    assert ok


@pytest.mark.parametrize("fs", ["exfat", "ntfs", "vfat", "fuseblk"])
def test_rsync_refuses_non_posix_filesystems(tmp_path, fs):
    ok, why = validate_target(str(tmp_path), "rsync", all_mounts, fstype=lambda p: fs)
    assert not ok and fs in why


def test_rsync_refuses_unknown_filesystem(tmp_path):
    ok, _ = validate_target(str(tmp_path), "rsync", all_mounts, fstype=lambda p: None)
    assert not ok


def test_btrfs_engine_accepts_non_btrfs_target_for_streams(tmp_path):
    ok, _ = validate_target(str(tmp_path), "btrfs", all_mounts, fstype=lambda p: "ext4")
    assert ok


def test_split_target_accepts_mount_or_suffixed(tmp_path):
    m1, r1 = split_target(str(tmp_path), "rsync")
    m2, r2 = split_target(str(tmp_path / "restore-tui"), "rsync")
    assert m1 == m2 == tmp_path and r1 == r2 == tmp_path / "restore-tui"
    assert split_target(str(tmp_path), "btrfs")[1] == tmp_path / "btrfs-restore"


def test_there_is_no_override_to_skip_the_mount_check(tmp_path):
    import inspect

    sig = inspect.signature(validate_target)
    assert not any("allow" in p or "force" in p or "unmounted" in p for p in sig.parameters)


def test_fstype_of_uses_findmnt_output(tmp_path, monkeypatch):
    monkeypatch.setattr(drives.shutil, "which", lambda n: "/usr/bin/findmnt")

    class R:
        stdout = "ext4\n"

    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    assert drives.fstype_of(tmp_path, run=run) == "ext4"
    assert seen["cmd"][:4] == ["findmnt", "-n", "-o", "FSTYPE"]


def test_fstype_of_without_findmnt_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(drives.shutil, "which", lambda n: None)
    assert drives.fstype_of(tmp_path) is None
