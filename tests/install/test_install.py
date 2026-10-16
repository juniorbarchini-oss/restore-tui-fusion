"""install.sh / uninstall.sh contra un prefijo temporal (DESTDIR) y un /opt falso: JAMÁS el /opt real.

Cubre: instala la app fusionada; retira las DOS instalaciones viejas archivándolas (nada se borra);
no toca config ni snapshots; config nueva con modo 600; no pisa comandos ajenos; es idempotente;
desinstala solo lo registrado; el chequeo de residuos recorre cada montaje (también /home aparte).
Sin red ni root: `--no-venv` (los shims caen a python del sistema) y DESTDIR evita paquetes del host.
"""

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import hermetic
import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def tree_hash(root: Path):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                stat.S_IMODE(p.stat().st_mode),
            )
        elif p.is_symlink():
            out[str(p.relative_to(root))] = os.readlink(p)
    return out


class Box:
    """Un sistema falso bajo tmp_path, con helpers para sembrar instalaciones viejas."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "sysroot"
        self.home = self.root / "home" / "tester"
        self.home.mkdir(parents=True)
        for d in ("opt", "usr/local/bin", "usr/share/applications"):
            (self.root / d).mkdir(parents=True)
        # PATH hermético: dobles inofensivos para sudo/pacman/apt-get/sshfs... + herramientas básicas.
        self.path, self.fakes = hermetic.hermetic_path(tmp_path, extra=("git",))
        assert hermetic.escaped_commands(self.path, self.fakes) == {}
        self.env = {
            "HERMETIC_LOG": str(tmp_path / "calls.log"),
            "HOME": str(self.home),
            "DESTDIR": str(self.root),
            "RF_USER": "tester",
            "RF_HOME": "/home/tester",
            "RF_TERMINAL": "foot",
            "RF_SNAP_DIR": "/.snapshots",
            "PATH": self.path,
        }

    def p(self, system_path):
        return self.root / system_path.lstrip("/")

    def old_install(self, name, pkg, desktop):
        d = self.p(f"/opt/{name}")
        (d / pkg).mkdir(parents=True)
        (d / pkg / "mod.py").write_text(f"# old {name}\n")
        (d / "bin").mkdir()
        (d / ".venv" / "bin").mkdir(parents=True)
        (d / ".venv" / "bin" / "python3").write_text("old venv")
        for c in ("restore-tui", "restore-now", "backup-now"):
            (d / "bin" / c).write_text("#!/bin/sh\n")
        self.p(f"/usr/share/applications/{desktop}").write_text("[Desktop Entry]\nName=old\n")
        return d

    def old_links(self, name):
        for c in ("restore-tui", "restore-now", "backup-now"):
            link = self.p(f"/usr/local/bin/{c}")
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(f"/opt/{name}/bin/{c}")

    def config(self, edition, text="PBS_PASSWORD=x\n", mode=0o644):
        d = self.home / ".config" / edition
        d.mkdir(parents=True, exist_ok=True)
        f = d / "config.conf"
        f.write_text(text)
        f.chmod(mode)
        return f

    def run(self, script, *args, env=None, stdin=subprocess.DEVNULL):
        return subprocess.run(
            ["bash", str(REPO / script), *args],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            stdin=stdin,
            timeout=120,
        )

    def install(self, *args, **kw):
        return self.run("install.sh", "--no-venv", *args, **kw)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def archives(box):
    d = box.p("/opt/restore-fusion-archive")
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# -- instalación limpia -----------------------------------------------------------------------------
def test_clean_install_layout(box):
    r = box.install("--yes")
    assert r.returncode == 0, r.stderr + r.stdout
    opt = box.p("/opt/restore-fusion")
    assert (opt / "restore_fusion" / "cli.py").is_file()
    assert (opt / "bin" / "restore-tui").stat().st_mode & 0o111
    assert not list(opt.rglob("__pycache__"))
    for c in ("restore-tui", "restore-now", "backup-now"):
        link = box.p(f"/usr/local/bin/{c}")
        assert link.is_symlink() and os.readlink(link) == f"/opt/restore-fusion/bin/{c}"
    assert box.p("/usr/share/applications/restore-fusion.desktop").is_file()
    assert "Exec=foot" in box.p("/usr/share/applications/restore-fusion.desktop").read_text()
    owned = (opt / ".owned-paths").read_text().split()
    assert "/opt/restore-fusion" in owned and "/usr/local/bin/backup-now" in owned
    assert not box.p("/opt/restore-fusion-archive").exists(), "nothing to retire: no archive"


def test_install_records_the_commit_for_the_update_check(box):
    r = box.install("--yes")
    assert r.returncode == 0, r.stderr + r.stdout
    version = (box.p("/opt/restore-fusion") / "VERSION").read_text().strip()
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert version == (head or "unknown")


def test_config_is_seeded_with_mode_600_when_none_exists(box):
    box.install("--yes")
    cfg = box.home / ".config" / "restore-tui" / "config.conf"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700
    text = cfg.read_text()
    assert "REMOTE_HOST=" in text or "REMOTE_HOST" in text


def test_shims_work_from_the_installed_tree(box):
    """Los comandos resuelven su venv/PYTHONPATH relativos a donde están instalados, no a /opt real."""
    box.install("--yes")
    shim = box.p("/opt/restore-fusion/bin/restore-tui")
    r = subprocess.run(
        ["bash", str(shim), "--paths"],
        env={**box.env, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "Paths this tool creates" in r.stdout


# -- retirar las dos instalaciones viejas ---------------------------------------------------------------
def test_both_old_installs_are_archived_not_deleted(box):
    bt = box.old_install("btrfs-restore-tui", "btrfs_restore", "btrfs-restore.desktop")
    ex = box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    box.old_links("btrfs-restore-tui")
    r = box.install("--yes")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not bt.exists() and not ex.exists()
    names = archives(box)
    arch_bt = [n for n in names if n.startswith("btrfs-restore-tui-2")]
    arch_ex = [n for n in names if n.startswith("restore-tui-2")]
    assert len(arch_bt) == 1 and len(arch_ex) == 1
    arch = box.p("/opt/restore-fusion-archive")
    assert (
        arch / arch_bt[0] / "btrfs_restore" / "mod.py"
    ).read_text() == "# old btrfs-restore-tui\n"
    assert (arch / arch_ex[0] / "restore_tui" / "mod.py").read_text() == "# old restore-tui\n"
    assert (arch / arch_bt[0] / ".venv" / "bin" / "python3").exists()
    # sus entradas de escritorio también se archivan, no se borran
    assert not box.p("/usr/share/applications/btrfs-restore.desktop").exists()
    assert not box.p("/usr/share/applications/restore-tui.desktop").exists()
    desk = [p for p in arch.rglob("*.desktop")]
    assert {p.name for p in desk} == {"btrfs-restore.desktop", "restore-tui.desktop"}
    # los comandos apuntan ahora a la app fusionada
    assert os.readlink(box.p("/usr/local/bin/restore-tui")) == "/opt/restore-fusion/bin/restore-tui"


def test_old_install_can_be_only_one_of_the_two(box):
    ex = box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    box.old_links("restore-tui")
    assert box.install("--yes").returncode == 0
    assert not ex.exists() and len(archives(box)) == 2  # el viejo + su .desktop


def test_legacy_loose_backup_now_script_is_archived(box):
    legacy = box.home / ".local" / "bin" / "backup-now"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\necho old loose script\n")
    assert box.install("--yes").returncode == 0
    assert not legacy.exists()
    kept = list(box.p("/opt/restore-fusion-archive").rglob("backup-now"))
    assert kept and "old loose script" in kept[0].read_text()


def test_existing_configs_and_snapshots_and_backups_are_never_touched(box):
    box.old_install("btrfs-restore-tui", "btrfs_restore", "btrfs-restore.desktop")
    box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    box.config("btrfs-restore", "TARGET_DIR=/x\n", 0o644)
    box.config("restore-tui", "ENGINE=rsync\n", 0o644)
    snaps = box.p("/.snapshots")
    (snaps / "root_20260910_000000").mkdir(parents=True)
    (snaps / "root_20260910_000000" / "f").write_text("data")
    usb = (
        box.root
        / "media"
        / "tester"
        / "USB_DATA"
        / "restore-tui"
        / "snapshots"
        / "2026-09-10_000000"
        / "home"
    )
    usb.mkdir(parents=True)
    (usb / "doc").write_text("backup")
    before = {
        "cfg": tree_hash(box.home / ".config"),
        "snaps": tree_hash(snaps),
        "usb": tree_hash(box.root / "media"),
    }
    r = box.install("--yes")
    assert r.returncode == 0, r.stderr
    assert (
        tree_hash(box.home / ".config") == before["cfg"]
    ), "config must be byte- and mode-identical"
    assert tree_hash(snaps) == before["snaps"]
    assert tree_hash(box.root / "media") == before["usb"]
    assert "not modified" in r.stdout and "mode 644" in r.stdout
    assert "were not touched" in r.stdout


def test_loose_config_mode_is_only_tightened_after_an_explicit_yes(box, tmp_path):
    cfg = box.config("restore-tui", "PBS_PASSWORD=x\n", 0o644)
    # --yes => No ; sin terminal => No
    box.install("--yes")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
    box.install()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
    # con un terminal que responde "y" sí se aprieta (simulado con un pty)
    import pty

    master, slave = pty.openpty()
    p = subprocess.Popen(
        ["bash", str(REPO / "install.sh"), "--no-venv"],
        env=box.env, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    os.write(master, b"y\n")
    out, _ = p.communicate(timeout=120)
    os.close(master)
    os.close(slave)
    assert p.returncode == 0, out
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_foreign_command_in_usr_local_bin_is_not_overwritten(box):
    foreign = box.p("/usr/local/bin/backup-now")
    foreign.write_text("#!/bin/sh\necho mine\n")
    r = box.install("--yes")
    assert r.returncode == 0
    assert foreign.read_text() == "#!/bin/sh\necho mine\n" and not foreign.is_symlink()
    assert "not ours" in r.stderr + r.stdout
    assert box.p("/usr/local/bin/restore-tui").is_symlink()  # los demás sí se instalan


def test_install_is_idempotent_and_does_not_archive_itself(box):
    box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    assert box.install("--yes").returncode == 0
    first = archives(box)
    state = tree_hash(box.p("/opt/restore-fusion"))
    assert box.install("--yes").returncode == 0
    assert archives(box) == first
    assert tree_hash(box.p("/opt/restore-fusion")) == state


def test_refuses_a_source_dir_that_is_not_the_fusion_checkout(box, tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    shutil.copy(REPO / "install.sh", fake / "install.sh")
    r = subprocess.run(
        ["bash", str(fake / "install.sh"), "--no-venv"], env=box.env, capture_output=True, text=True
    )
    assert r.returncode == 1 and "does not look like" in r.stderr


def test_unknown_option_is_rejected(box):
    assert box.install("--wat").returncode == 2


def test_never_touches_the_real_opt(box):
    """El DESTDIR contiene todo; nada nuevo aparece fuera de él."""
    before = set(os.listdir("/opt")) if os.path.isdir("/opt") else set()
    box.install("--yes")
    after = set(os.listdir("/opt")) if os.path.isdir("/opt") else set()
    assert before == after


# -- desinstalar -----------------------------------------------------------------------------------------------------------
def test_uninstall_removes_only_the_app_and_keeps_config_snapshots_archive(box):
    box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    cfg = box.config("restore-tui", "ENGINE=rsync\n", 0o600)
    snap = box.p("/.snapshots/root_20260910_000000")
    snap.mkdir(parents=True)
    box.install("--yes")
    r = box.run("uninstall.sh", "--yes")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not box.p("/opt/restore-fusion").exists()
    for c in ("restore-tui", "restore-now", "backup-now"):
        assert (
            not box.p(f"/usr/local/bin/{c}").exists()
            and not box.p(f"/usr/local/bin/{c}").is_symlink()
        )
    assert not box.p("/usr/share/applications/restore-fusion.desktop").exists()
    assert cfg.read_text() == "ENGINE=rsync\n"
    assert snap.is_dir()
    assert box.p("/opt/restore-fusion-archive").is_dir()
    assert "Kept on purpose" in r.stdout and "(local snapshot)" in r.stdout


def test_uninstall_never_deletes_snapshots_with_yes_or_without_a_tty(box):
    box.install("--yes")
    snap = box.p("/.snapshots/home_20260910_000000")
    snap.mkdir(parents=True)
    (snap / "f").write_text("x")
    # aunque stdin diga DELETE, sin terminal es No
    r = subprocess.run(
        ["bash", str(REPO / "uninstall.sh")],
        env=box.env,
        input="DELETE\n",
        capture_output=True,
        text=True,
    )
    assert (snap / "f").exists(), r.stdout
    r = box.run("uninstall.sh", "--yes", "--purge")
    assert (snap / "f").exists()


def test_purge_with_yes_does_not_remove_the_config(box):
    cfg = box.config("restore-tui", "ENGINE=rsync\n", 0o600)
    box.install("--yes")
    box.run("uninstall.sh", "--yes", "--purge")
    assert cfg.exists()


def test_uninstall_leaves_a_foreign_command_alone(box):
    box.install("--yes")
    link = box.p("/usr/local/bin/restore-now")
    link.unlink()
    link.write_text("#!/bin/sh\nmine\n")
    box.run("uninstall.sh", "--yes")
    assert link.read_text() == "#!/bin/sh\nmine\n"


def test_uninstall_without_an_install_record_removes_nothing(box):
    stray = box.p("/opt/restore-fusion")
    (stray / "restore_fusion").mkdir(parents=True)
    (stray / "restore_fusion" / "cli.py").write_text("")
    r = box.run("uninstall.sh", "--yes")
    assert stray.exists() and "nothing was removed" in r.stderr


# -- residuos (R12) ----------------------------------------------------------------------------------------------------------
def test_residue_check_searches_every_mount_including_a_separate_home(box):
    """`find / -xdev` se detiene en el límite de /home. Se busca en cada montaje por separado."""
    box.install("--yes")
    stray = box.home / ".cache" / "restore-fusion-leftover"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    r = box.run("uninstall.sh", "--yes", env={"RF_MOUNTS": "/ /home"})
    assert "/home/tester/.cache/restore-fusion-leftover" in r.stdout
    assert "Unexpected leftovers" in r.stdout
    assert "Searched these filesystems for leftovers: / /home" in r.stdout


def test_residue_check_reports_clean_when_only_kept_things_remain(box):
    box.old_install("restore-tui", "restore_tui", "restore-tui.desktop")
    box.config("restore-tui", "ENGINE=rsync\n", 0o600)
    box.install("--yes")
    r = box.run("uninstall.sh", "--yes", env={"RF_MOUNTS": "/ /home"})
    assert "Unexpected leftovers" not in r.stdout
    assert "Only what you chose to keep" in r.stdout


# -- calidad de los scripts ---------------------------------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize(
    "script", ["install.sh", "uninstall.sh", "bin/restore-tui", "bin/restore-now", "bin/backup-now"]
)
def test_shellcheck(script):
    r = subprocess.run(
        ["shellcheck", "-S", "warning", str(REPO / script)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_scripts_have_no_unsafe_shell_patterns(script):
    text = (REPO / script).read_text()
    assert "StrictHostKeyChecking=no" not in text
    assert "rm -rf /" not in text.replace("rm -rf -- ", "")
    assert "btrfs subvolume delete" not in (REPO / "install.sh").read_text()


def test_install_and_uninstall_never_call_a_package_manager_or_sudo(box, tmp_path):
    box.install("--yes")
    box.run("uninstall.sh", "--yes")
    log = tmp_path / "calls.log"
    calls = log.read_text() if log.exists() else ""
    for name in ("pacman", "apt-get", "dnf", "sudo", "sshfs", "pip", "systemctl"):
        assert not any(line.startswith(name + " ") for line in calls.splitlines()), calls
