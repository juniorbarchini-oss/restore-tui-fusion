"""detect.py: tabla de elección de motor y de gestor de paquetes."""

from pathlib import Path

import pytest

from restore_fusion.detect import (
    EngineError,
    choose_engine,
    detect_package_manager,
    is_btrfs_subvolume,
    source_is_btrfs,
)


def mounted(_p):
    return True


EXT4 = lambda p: "ext4"  # noqa: E731


@pytest.mark.parametrize(
    "setting,src_btrfs,tool,expected",
    [
        ("auto", True, True, "btrfs"),
        ("auto", False, True, "rsync"),
        ("auto", False, False, "rsync"),
        ("rsync", True, True, "rsync"),  # solo con ENGINE=rsync explícito
        ("rsync", False, False, "rsync"),
        ("btrfs", True, True, "btrfs"),
        ("", True, True, "btrfs"),  # vacío = auto
        ("AUTO", False, True, "rsync"),
    ],
)
def test_engine_choice_table(setting, src_btrfs, tool, expected):
    got = choose_engine(setting, source_btrfs=src_btrfs, btrfs_tool=tool)
    assert got.engine == expected and got.reason


def test_auto_on_btrfs_without_tool_fails_instead_of_switching():
    with pytest.raises(EngineError, match="ENGINE=rsync"):
        choose_engine("auto", source_btrfs=True, btrfs_tool=False)


def test_explicit_btrfs_without_tool_fails():
    with pytest.raises(EngineError):
        choose_engine("btrfs", source_btrfs=True, btrfs_tool=False)


def test_explicit_btrfs_on_non_btrfs_source_fails():
    with pytest.raises(EngineError):
        choose_engine("btrfs", source_btrfs=False, btrfs_tool=True)


def test_invalid_setting_fails():
    with pytest.raises(EngineError):
        choose_engine("zfs", source_btrfs=False, btrfs_tool=False)


def test_target_is_validated_with_the_chosen_engine(tmp_path):
    with pytest.raises(EngineError, match="not a mount point"):
        choose_engine(
            "rsync",
            source_btrfs=False,
            btrfs_tool=False,
            target_dir=str(tmp_path),
            is_mount=lambda p: False,
            fstype=EXT4,
        )


def test_rsync_engine_refuses_exfat_target(tmp_path):
    with pytest.raises(EngineError, match="exfat"):
        choose_engine(
            "auto",
            source_btrfs=False,
            btrfs_tool=False,
            target_dir=str(tmp_path),
            is_mount=mounted,
            fstype=lambda p: "exfat",
        )


def test_btrfs_engine_accepts_ext4_target(tmp_path):
    got = choose_engine(
        "auto",
        source_btrfs=True,
        btrfs_tool=True,
        target_dir=str(tmp_path),
        is_mount=mounted,
        fstype=EXT4,
    )
    assert got.engine == "btrfs"


def test_no_target_means_no_target_validation():
    assert choose_engine("auto", source_btrfs=False, btrfs_tool=False).engine == "rsync"


# ---- origen ---------------------------------------------------------------


class _St:
    def __init__(self, ino):
        self.st_ino = ino


def test_subvolume_detection():
    fs = lambda p: "btrfs"  # noqa: E731
    assert is_btrfs_subvolume(Path("/"), fs, lambda p: _St(256))
    assert not is_btrfs_subvolume(Path("/"), fs, lambda p: _St(1234))
    assert not is_btrfs_subvolume(Path("/"), lambda p: "ext4", lambda p: _St(256))


def test_subvolume_stat_error_is_false():
    def boom(_p):
        raise OSError("nope")

    assert not is_btrfs_subvolume(Path("/"), lambda p: "btrfs", boom)


def test_source_is_btrfs_needs_every_mount():
    assert source_is_btrfs(("/", "/home"), lambda p: True)
    assert not source_is_btrfs(("/", "/home"), lambda p: str(p) == "/")


# ---- gestor de paquetes ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ID=ubuntu\nID_LIKE=debian\n", "apt"),
        ("ID=debian\n", "apt"),
        ('ID="linuxmint"\nID_LIKE="ubuntu debian"\n', "apt"),
        ("ID=arch\n", "pacman"),
        ("ID=omarchy\nID_LIKE=arch\n", "pacman"),
        ('ID=cachyos\nID_LIKE="arch"\n', "pacman"),
        ("ID=fedora\n", None),
        ("", None),
    ],
)
def test_package_manager_from_os_release(text, expected):
    assert detect_package_manager(text) == expected


def test_btrfs_ubuntu_is_a_valid_combination():
    # motor y gestor de paquetes son independientes
    eng = choose_engine("auto", source_btrfs=True, btrfs_tool=True).engine
    assert eng == "btrfs" and detect_package_manager("ID=ubuntu\nID_LIKE=debian\n") == "apt"
