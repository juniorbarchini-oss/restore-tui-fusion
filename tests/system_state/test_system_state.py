"""SystemStateCollector + gestores de paquetes. Portado de los dos system_state de las fuentes
(btrfs-restore-tui/tests/test_system_state.py y restore-tui/tests/test_system_state.py)."""

import json
import os
import shutil
import subprocess

import pytest

from conftest import fake_run_factory
from restore_fusion.packages.apt import DRIVER_EXCLUDE_PATTERNS
from restore_fusion.packages.pacman import DRIVER_RE

BOTH = [("apt", "rsync"), ("pacman", "btrfs"), ("apt", "btrfs"), ("pacman", "rsync")]


@pytest.mark.parametrize("pm,engine", BOTH)
def test_collect_all_is_best_effort_and_writes_core_files(make_collector, snap, pm, engine):
    warnings = make_collector(pm, engine).collect_all()
    assert isinstance(warnings, list)
    info = json.loads((snap / "_system_state" / "os_info.json").read_text())
    assert "hostname" in info and info["package_manager"] == pm and info["engine"] == engine
    rs = snap / "restore.sh"
    assert rs.exists() and rs.stat().st_mode & 0o111


@pytest.mark.parametrize("pm,engine", BOTH)
def test_restore_script_is_valid_bash(make_collector, snap, pm, engine):
    make_collector(pm, engine).collect_all()
    rc = subprocess.run(["bash", "-n", str(snap / "restore.sh")], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


@pytest.mark.parametrize("pm,engine", BOTH)
def test_restore_script_passes_shellcheck(make_collector, snap, pm, engine):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    make_collector(pm, engine).collect_all()
    rc = subprocess.run(
        ["shellcheck", "-S", "error", str(snap / "restore.sh")], capture_output=True, text=True
    )
    assert rc.returncode == 0, rc.stdout


def test_a_failing_step_becomes_a_warning_not_a_crash(make_collector, snap):
    c = make_collector("pacman", "btrfs")
    steps = c.pm.capture_steps()

    def boom(ctx):
        raise RuntimeError("boom")

    c.pm.capture_steps = lambda: [("pacman_packages", boom), *steps[1:]]
    warnings = c.collect_all()
    assert any("pacman_packages: boom" in w for w in warnings)
    assert (snap / "restore.sh").exists()  # los pasos siguientes corrieron


# ---- pacman (btrfs-restore-tui) -------------------------------------------------------


def test_explicit_pkglist_excludes_aur_packages(make_collector, snap):
    """`pacman -Qqe` lista también los de AUR: pkglist_explicit.txt es solo de repo, para que el
    `pacman -S` del restore.sh no choque con "target not found" (hallazgo real #16)."""
    run = fake_run_factory({
        ("pacman", "-Qqm"): "yay\nclaude-desktop-extra\n",
        ("pacman", "-Qqe"): "yay\nclaude-desktop-extra\ngit\nfirefox\n",
    })  # fmt: skip
    c = make_collector("pacman", "btrfs", run=run, which=lambda n: "/usr/bin/pacman")
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.pacman import capture_pacman_packages

    capture_pacman_packages(c.ctx)
    explicit = (snap / "_system_state" / "pkglist_explicit.txt").read_text().split()
    aur = (snap / "_system_state" / "pkglist_aur.txt").read_text().split()
    assert sorted(explicit) == ["firefox", "git"]
    assert "yay" in aur and "claude-desktop-extra" in aur and not set(explicit) & set(aur)


def test_pacman_missing_is_a_warning(make_collector):
    c = make_collector("pacman", "btrfs")
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.pacman import capture_pacman_packages

    capture_pacman_packages(c.ctx)
    assert any("pacman not found" in w for w in c.warnings)


def test_pacman_etc_and_config_and_bootloader(make_collector, snap, roots):
    (roots.etc / "pacman.conf").write_text("[options]\n")
    (roots.etc / "pacman.d").mkdir()
    (roots.etc / "pacman.d" / "mirrorlist").write_text("Server = x\n")
    (roots.etc / "hosts").write_text("127.0.0.1 localhost\n")
    (roots.etc / "mkinitcpio.conf").write_text("HOOKS=()\n")
    (roots.etc / "keyd").mkdir()
    (roots.etc / "keyd" / "default.conf").write_text("[ids]\n")
    (roots.boot / "limine.conf").write_text("timeout: 5\n")
    make_collector("pacman", "btrfs").collect_all()
    st = snap / "_system_state"
    assert (st / "pacman" / "pacman.conf").exists()
    assert (st / "pacman" / "pacman.d" / "mirrorlist").exists()
    assert (st / "etc" / "hosts").exists() and (st / "etc" / "mkinitcpio.conf").exists()
    assert (st / "keyd" / "default.conf").exists()
    assert "limine" in json.loads((st / "bootloader.json").read_text())
    assert (st / "bootloader" / "limine" / "limine.conf").exists()


def test_driver_packages_are_filtered():
    for pkg in ("nvidia", "nvidia-utils", "linux", "linux-firmware", "amd-ucode", "intel-ucode"):
        assert any(rx.match(pkg) for rx in DRIVER_RE), pkg
    for pkg in ("firefox", "git", "hyprland"):
        assert not any(rx.match(pkg) for rx in DRIVER_RE), pkg


# ---- apt (restore-tui) ---------------------------------------------------------------------


def test_keyrings_dont_collide(make_collector, snap, roots):
    (roots.etc / "apt" / "keyrings").mkdir(parents=True)
    (roots.usr / "share" / "keyrings").mkdir(parents=True)
    (roots.etc / "apt" / "keyrings" / "a.gpg").write_bytes(b"ETC")
    (roots.usr / "share" / "keyrings" / "b.gpg").write_bytes(b"USRSHARE")
    c = make_collector("apt", "rsync")
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.apt import capture_apt_sources

    capture_apt_sources(c.ctx)
    dest = snap / "_system_state" / "apt_sources"
    assert (dest / "keyrings" / "a.gpg").read_bytes() == b"ETC"
    assert (dest / "usr_share_keyrings" / "b.gpg").read_bytes() == b"USRSHARE"


def test_usr_local_bin_symlinks_reported_not_silently_dropped(
    make_collector, snap, roots, tmp_path
):
    ulb = roots.usr / "local" / "bin"
    (ulb / "smartlinux").write_text("#!/bin/sh\necho hi\n")
    opt = tmp_path / "opt_app" / "app.AppImage"
    opt.parent.mkdir(parents=True)
    opt.write_text("binary")
    (ulb / "myapp").symlink_to(opt)
    make_collector("apt", "rsync").collect_all()
    state = snap / "_system_state"
    assert (state / "usr_local_bin" / "smartlinux").is_file()
    assert not (state / "usr_local_bin" / "myapp").exists()
    txt = (state / "usr_local_bin_symlinks.txt").read_text()
    assert "myapp" in txt and str(opt) in txt


def test_gnome_extensions_skipped_without_binary(make_collector, snap):
    c = make_collector("apt", "rsync", which=lambda n: None)
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.desktop import capture_gnome_extensions

    capture_gnome_extensions(c.ctx)
    assert not (snap / "_system_state" / "gnome_extensions_enabled.txt").exists()


def test_gnome_extensions_captured_when_available(make_collector, snap):
    run = fake_run_factory({("gnome-extensions",): "user@ext.example.com\nother@ext.example.com\n"})
    c = make_collector("apt", "rsync", run=run, which=lambda n: "/usr/bin/" + n)
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.desktop import capture_gnome_extensions

    capture_gnome_extensions(c.ctx)
    assert (
        "user@ext.example.com"
        in (snap / "_system_state" / "gnome_extensions_enabled.txt").read_text()
    )


def test_apt_manual_packages_filter_drivers(make_collector, snap):
    run = fake_run_factory({("apt-mark",): "firefox\nnvidia-driver-550\nlinux-generic\ngit\n"})
    c = make_collector("apt", "rsync", run=run, which=lambda n: "/usr/bin/" + n)
    c.meta_dir.mkdir(parents=True)
    from restore_fusion.packages.apt import capture_apt_packages

    capture_apt_packages(c.ctx)
    assert (snap / "_system_state" / "apt_packages_manual.txt").read_text().split() == [
        "firefox",
        "git",
    ]
    assert any(p.match("nvidia-driver-550") for p in DRIVER_EXCLUDE_PATTERNS)


def test_crontab_runs_as_the_real_user_under_sudo(make_collector, snap, monkeypatch):
    seen = []

    def run(cmd, timeout=60):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="0 * * * * echo hi\n", stderr="")

    monkeypatch.setenv("SUDO_USER", "alice")
    c = make_collector("apt", "rsync", run=run, which=lambda n: "/usr/bin/" + n)
    c.meta_dir.mkdir(parents=True)
    c._export_crontab()
    assert seen[0] == ["runuser", "-u", "alice", "--", "crontab", "-l"]
    assert (snap / "_system_state" / "crontab.txt").exists()
