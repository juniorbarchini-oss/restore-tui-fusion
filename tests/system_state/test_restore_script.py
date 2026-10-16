"""restore.sh compuesto por fragmentos: pruebas de oro, lista de pendientes en
las dos distros y ejecución real del script con binarios falsos."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import hermetic
import pytest

from restore_fusion.packages import AptManager, PacmanManager
from restore_fusion.system_state.restore_script import (
    compose,
    fragments_for,
    leftovers_checklist_present,
    strip_markers,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
ALL = [(pm, e) for pm in ("apt", "pacman") for e in ("rsync", "btrfs")]


# ---- oro: texto de referencia del restore.sh ------------------------------------------------------


def test_apt_rsync_matches_the_golden_file():
    assert strip_markers(compose("apt", "rsync")) == (GOLDEN / "restore_apt_rsync.sh").read_text()


def test_pacman_btrfs_matches_the_golden_file():
    assert (
        strip_markers(compose("pacman", "btrfs"))
        == (GOLDEN / "restore_pacman_btrfs.sh").read_text()
    )


def test_fragments_concatenate_to_the_whole_original():
    for pm, eng, gold in (
        ("apt", "rsync", "restore_apt_rsync.sh"),
        ("pacman", "btrfs", "restore_pacman_btrfs.sh"),
    ):
        old = "".join(
            (ROOT / "restore_fusion/system_state/templates" / rel).read_text()
            for _f, rel, new in fragments_for(pm, eng)
            if not new
        )
        assert old == (GOLDEN / gold).read_text()


# ---- marcas ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("pm,engine", ALL)
def test_markers_are_well_formed_and_the_shebang_is_first(pm, engine):
    s = compose(pm, engine)
    assert s.startswith("#!/usr/bin/env bash\n")
    begins = re.findall(r"^# @@new-begin (\S+)$", s, re.M)
    ends = re.findall(r"^# @@new-end (\S+)$", s, re.M)
    assert begins == ends and len(set(begins)) == len(begins)
    assert "@@" not in strip_markers(s)
    assert strip_markers(strip_markers(s)) == strip_markers(s)


def test_unknown_pm_or_engine_raise():
    with pytest.raises(ValueError):
        compose("dnf", "rsync")
    with pytest.raises(ValueError):
        compose("apt", "zfs")


# ---- standalone bash + lista "software to install" para las dos distros ----------------------------------


@pytest.mark.parametrize("pm,engine", ALL)
def test_both_distros_end_with_the_leftovers_checklist(pm, engine):
    s = compose(pm, engine)
    assert leftovers_checklist_present(s)
    body = re.sub(r"^# @@.*\n", "", s, flags=re.M).rstrip()
    assert body.endswith("fi")  # la lista es lo último que imprime
    done = "RESTORE COMPLETE" if pm == "apt" else "RECOVERY COMPLETE"
    assert body.index(done) < body.rindex("SOFTWARE TO INSTALL")


@pytest.mark.parametrize("pm,engine", ALL)
def test_script_is_standalone_bash(pm, engine):
    s = compose(pm, engine)
    for forbidden in ("python3 -m", "restore_fusion", "restore_tui", "btrfs_restore"):
        # (el os_info.json se lee con python3 -c en la edición pacman: ya era así)
        assert forbidden not in s.replace("restore_fusion/cli.py", "").replace(
            "restore_tui/cli.py", ""
        ), forbidden
    assert "import restore" not in s
    rc = subprocess.run(["bash", "-n"], input=s, text=True, capture_output=True)
    assert rc.returncode == 0, rc.stderr


@pytest.mark.parametrize("pm,engine", ALL)
def test_shellcheck_error_level(pm, engine, tmp_path):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    p = tmp_path / "restore.sh"
    p.write_text(compose(pm, engine))
    rc = subprocess.run(["shellcheck", "-S", "error", str(p)], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout


def test_package_managers_expose_their_leftovers_fragments():
    for mgr in (AptManager(), PacmanManager()):
        ids = mgr.leftovers_fragment_ids()
        assert ids and "TODO" in mgr.leftovers_fragment("rsync" if mgr.id == "apt" else "btrfs")


def test_piezas_conservadas_de_las_fuentes():
    apt = compose("apt", "rsync")
    for marker in (
        "usr_share_keyrings",
        'TODO+=("apt install',
        'TODO+=("snap install',
        "usr_local_bin_symlinks.txt",
        "gnome_extensions_enabled.txt",
        "SOFTWARE TO INSTALL",
        'sudo apt-get install -y "$pkg"',
        "snap install obsidian --classic",
        'sudo snap install "$name" --classic',
        "SELF_REINSTALLED",
        "restore_tui/cli.py",
    ):
        assert marker in apt, marker
    pac = compose("pacman", "btrfs")
    for marker in (
        "OMARCHY_ALLOW_DIRECT_PACMAN=1",
        "command -v omarchy",
        "--answerclean None",
        "--answerdiff None",
        "--skipreview",
        "retry manually",
        '[ "$(id -u)" -ne 0 ]',
        "exec sudo -E",
        "--root",
        "--no-home",
        "arch-chroot",
        "btrfs receive",
    ):
        assert marker in pac, marker


def test_the_fused_app_is_reinstalled_after_the_old_one_so_it_wins():
    for pm in ("apt", "pacman"):
        s = compose(pm, "rsync")
        assert "restore-tui-fusion" in s and "restore_fusion/cli.py" in s
        # Linux layout first (~/desarrollo), macOS layout as fallback
        assert s.index("/desarrollo/restore-tui-fusion") < s.index(
            "/Documents/desarrollo/restore-tui-fusion"
        )
    apt = compose("apt", "rsync")
    assert apt.index("SELF_REINSTALLED=false") < apt.index("restore_fusion/cli.py")


# ---- ejecución real con binarios falsos ----------------------------------------------------------------------


SUDO = 'while [ $# -gt 0 ]; do case "$1" in -E|-n|-S) shift;; -u) shift 2;; *) break;; esac; done; exec "$@"\n'


def _fake_bin(d: Path, scripts: dict) -> Path:
    """Dir de falsos: un doble inofensivo por cada comando peligroso + los `scripts` del test."""
    return hermetic.fake_bin(d, scripts)


def _run(script: str, snap: Path, bin_dir: Path, args=(), stdin="yes\n", home=None):
    sh = snap / "restore.sh"
    sh.write_text(script)
    sh.chmod(0o755)
    path = f"{bin_dir}{os.pathsep}{hermetic.safe_dir(bin_dir.parent / 'safebin')}"
    escaped = hermetic.escaped_commands(path, bin_dir)
    assert not escaped, f"PATH leaks host commands: {escaped}"
    env = {
        "PATH": path,
        "HERMETIC_LOG": str(bin_dir.parent / "calls.log"),
        "HOME": str(home or snap.parent / "h"),
        "USER": "tester",
        "SUDO_USER": "tester",
    }
    return subprocess.run(
        ["bash", str(sh), *args], input=stdin, text=True, capture_output=True, env=env, timeout=60
    )


@pytest.mark.needs_rsync
def test_apt_script_restores_home_and_lists_what_failed(tmp_path):
    snap = tmp_path / "snap"
    (snap / "_system_state").mkdir(parents=True)
    (snap / "home" / "Documents").mkdir(parents=True)
    (snap / "home" / "Documents" / "a.txt").write_text("hello")
    (snap / "_system_state" / "apt_packages_manual.txt").write_text("good-pkg\nunknown-pkg\n")
    (snap / "_system_state" / "usr_local_bin_symlinks.txt").write_text("myapp -> /opt/x/app\n")
    (snap / "_system_state" / "gnome_extensions_enabled.txt").write_text("ext@example\n")
    home = tmp_path / "h"
    home.mkdir()
    bin_dir = _fake_bin(
        tmp_path / "bin",
        {
            "sudo": SUDO,
            "apt-get": '[ "$1 $2" = "install -y" ] && [ "$3" = "unknown-pkg" ] && exit 100; exit 0\n',
        },
    )
    r = _run(compose("apt", "rsync"), snap, bin_dir, home=home)
    assert r.returncode == 0, r.stderr
    assert (home / "Documents" / "a.txt").read_text() == "hello"  # los datos primero
    out = r.stdout
    assert "SOFTWARE TO INSTALL" in out and "apt install unknown-pkg" in out
    assert "apt install good-pkg" not in out and "reinstall: myapp -> /opt/x/app" in out
    assert "ext@example" in out


@pytest.mark.parametrize("script_engine", ["rsync", "btrfs"])
@pytest.mark.needs_rsync
def test_pacman_script_lists_what_was_not_installed(tmp_path, script_engine):
    snap = tmp_path / "snap"
    (snap / "_system_state").mkdir(parents=True)
    st = snap / "_system_state"
    (st / "os_info.json").write_text('{"user": "tester"}')
    (st / "pkglist_explicit.txt").write_text("git\nmissing-repo-pkg\n")
    (st / "pkglist_aur.txt").write_text("some-aur-pkg\n")
    (st / "flatpak_list.txt").write_text("org.example.App\n")
    bin_dir = _fake_bin(
        tmp_path / "bin",
        {
            "sudo": SUDO,
            "pacman": 'if [ "$1" = "-Qq" ] && [ $# -eq 1 ]; then echo git; exit 0; fi; '
            '[ "$1" = "-S" ] && exit 1; exit 0\n',
            "flatpak": '[ "$1" = "info" ] && exit 1; exit 0\n',
        },
    )
    r = _run(compose("pacman", script_engine), snap, bin_dir, args=["--no-home", "--no-etc"])
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "SOFTWARE TO INSTALL" in out
    assert "pacman -S missing-repo-pkg" in out and "AUR: yay -S some-aur-pkg" in out
    assert "flatpak install flathub org.example.App" in out
    assert "pacman -S git" not in out  # lo instalado no se lista


@pytest.mark.needs_rsync
def test_pacman_script_prints_no_checklist_when_everything_is_installed(tmp_path):
    snap = tmp_path / "snap"
    st = snap / "_system_state"
    st.mkdir(parents=True)
    (st / "os_info.json").write_text('{"user": "tester"}')
    (st / "pkglist_explicit.txt").write_text("git\n")
    bin_dir = _fake_bin(
        tmp_path / "bin",
        {"sudo": SUDO, "pacman": 'if [ "$1" = "-Qq" ]; then echo git; fi; exit 0\n'},
    )
    r = _run(compose("pacman", "btrfs"), snap, bin_dir, args=["--no-home", "--no-etc"])
    assert r.returncode == 0 and "SOFTWARE TO INSTALL" not in r.stdout


@pytest.mark.needs_rsync
def test_pacman_rsync_home_restore(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    (snap / "_system_state").mkdir(parents=True)
    (snap / "_system_state" / "os_info.json").write_text('{"user": "tester"}')
    (snap / "home" / "Documents").mkdir(parents=True)
    (snap / "home" / "Documents" / "a.txt").write_text("hello")
    root = tmp_path / "newroot"
    (root / "home" / "tester").mkdir(parents=True)
    bin_dir = _fake_bin(
        tmp_path / "bin",
        {
            "sudo": SUDO,
            "arch-chroot": 'shift; exec "$@"\n',
            "pacman": "exit 0\n",
            "chown": "exit 0\n",
        },
    )
    r = _run(
        compose("pacman", "rsync"),
        snap,
        bin_dir,
        args=["--root", str(root), "--no-etc", "--no-packages"],
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "home" / "tester" / "Documents" / "a.txt").read_text() == "hello"


@pytest.mark.needs_rsync
def test_apt_btrfs_subvolume_home_restore(tmp_path):
    snap = tmp_path / "snap"
    (snap / "_system_state").mkdir(parents=True)
    (snap / "home_20260101_000000" / "tester" / "Documents").mkdir(parents=True)
    (snap / "home_20260101_000000" / "tester" / "Documents" / "a.txt").write_text("hello")
    home = tmp_path / "h"
    home.mkdir()
    bin_dir = _fake_bin(tmp_path / "bin", {"sudo": SUDO})
    r = _run(compose("apt", "btrfs"), snap, bin_dir, home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "Documents" / "a.txt").read_text() == "hello"


# ---- guarda de hermeticidad ---------------------------------------------------------------------------------------


def test_guard_dangerous_commands_only_resolve_inside_the_fake_dir(tmp_path):
    """Si algún comando peligroso (gestores de paquetes, sudo...) se resolviera fuera del dir de falsos,
    en una máquina con `yay` o `sudo` reales el script los llamaría. Se comprueba con el PATH real de _run
    y desde el propio bash."""
    bin_dir = _fake_bin(tmp_path / "bin", {"sudo": SUDO})
    path = f"{bin_dir}{os.pathsep}{hermetic.safe_dir(tmp_path / 'safebin')}"
    assert hermetic.escaped_commands(path, bin_dir) == {}
    for name in hermetic.DANGEROUS:
        r = subprocess.run(
            ["bash", "-c", f"command -v {name}"], env={"PATH": path}, capture_output=True, text=True
        )
        assert r.stdout.strip() == str(bin_dir / name), name


def test_guard_detects_a_leaky_path(tmp_path):
    """El detector funciona: un `yay` "del host" (que se cuelga) al final del PATH se marca como filtrado
    si el dir de falsos no lo tapa, y no se marca si lo tapa."""
    host = hermetic.fake_bin(tmp_path / "host", {"yay": "sleep 600\n"}, stubs=())
    fakes_without_yay = hermetic.fake_bin(tmp_path / "bin", {}, stubs=("sudo",))
    leaky = f"{fakes_without_yay}{os.pathsep}{host}"
    assert hermetic.escaped_commands(leaky, fakes_without_yay) == {"yay": str(host / "yay")}
    fakes_full = hermetic.fake_bin(tmp_path / "bin2", {})
    assert hermetic.escaped_commands(f"{fakes_full}{os.pathsep}{host}", fakes_full) == {}


def test_the_safe_dir_never_links_a_dangerous_command(tmp_path):
    safe = hermetic.safe_dir(tmp_path / "safebin")
    assert not {p.name for p in safe.iterdir()} & set(hermetic.DANGEROUS)
    with pytest.raises(ValueError):
        hermetic.safe_dir(tmp_path / "x", extra=["sudo"])


def test_a_real_yay_on_the_host_is_never_called(tmp_path):
    """Regresión del cuelgue: aunque el host tenga un `yay`, el script usa el doble y lo deja en el log."""
    snap = tmp_path / "snap"
    st = snap / "_system_state"
    st.mkdir(parents=True)
    (st / "os_info.json").write_text('{"user": "tester"}')
    (st / "pkglist_aur.txt").write_text("some-aur-pkg\n")
    bin_dir = _fake_bin(tmp_path / "bin", {"sudo": SUDO, "pacman": "exit 0\n"})
    r = _run(compose("pacman", "btrfs"), snap, bin_dir, args=["--no-home", "--no-etc"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "yay -S --needed" in (tmp_path / "calls.log").read_text()


def test_fake_id_says_root_for_u_and_delegates_the_rest(tmp_path):
    """Como usuario normal, `id -u` != 0 hace que restore.sh ejecute `exec sudo -E "$0"`; con un sudo falso
    que reejecuta el script eso es un bucle infinito. El `id` falso lo evita."""
    bin_dir = _fake_bin(tmp_path / "bin", {})
    path = f"{bin_dir}{os.pathsep}{hermetic.safe_dir(tmp_path / 'safebin')}"

    def idout(*a):
        return subprocess.run(
            ["id", *a], env={"PATH": path}, capture_output=True, text=True
        ).stdout.strip()

    assert idout("-u") == "0" and idout("-ru") == "0"
    assert (
        idout("-un") == subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    )


def test_script_does_not_reexec_under_sudo_even_when_not_root(tmp_path, monkeypatch):
    """Con un sudo falso que se re-ejecuta, un script que creyera no ser root nunca terminaría."""
    snap = tmp_path / "snap"
    st = snap / "_system_state"
    st.mkdir(parents=True)
    (st / "os_info.json").write_text('{"user": "tester"}')
    bin_dir = _fake_bin(
        tmp_path / "bin", {"sudo": 'echo SUDO_CALLED >> "$HERMETIC_LOG"; exec "$@"\n'}
    )
    r = _run(
        compose("pacman", "btrfs"), snap, bin_dir, args=["--no-home", "--no-etc", "--no-packages"]
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "re-running with sudo" not in r.stdout
