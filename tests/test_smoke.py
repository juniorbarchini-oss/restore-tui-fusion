"""Humo: el paquete importa y los paquetes viejos no son importables."""

import pytest

import restore_fusion


def test_package_imports():
    assert restore_fusion.__version__


def test_old_packages_are_not_importable():
    for name in ("restore_tui", "btrfs_restore"):
        with pytest.raises(ImportError):
            __import__(name)


@pytest.mark.needs_rsync
def test_marker_needs_rsync_skips_when_missing():
    import shutil

    assert shutil.which("rsync")


@pytest.mark.needs_ssh
def test_marker_needs_ssh_skips_when_missing():
    import shutil

    assert shutil.which("ssh")


@pytest.mark.needs_btrfs
def test_marker_needs_btrfs_skips_when_missing():
    import shutil

    assert shutil.which("btrfs")


@pytest.mark.needs_root_btrfs
def test_marker_root_btrfs_skips_by_default():
    pytest.fail("real btrfs e2e must never run in the default suite")


@pytest.mark.hardware
def test_marker_hardware_is_always_skipped():
    pytest.fail("hardware tests are run by hand")


def test_hardware_folder_is_not_skipped_by_its_name():
    """pytest puts folder names in item.keywords: a test under tests/hardware/ carries "hardware" there
    without the marker. Only the real marker may trigger the always-skip."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_root_conftest", Path(__file__).with_name("conftest.py")
    )
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    class FakeItem:
        keywords = {"hardware": True, "test_e2e_btrfs.py": True}  # folder name, no marker
        added = []

        def get_closest_marker(self, name):
            return None

        def add_marker(self, marker):
            self.added.append(marker)

    item = FakeItem()
    conftest.pytest_collection_modifyitems(None, [item])
    assert not any("run by hand" in str(m.kwargs.get("reason", "")) for m in item.added)
