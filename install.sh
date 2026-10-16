#!/usr/bin/env bash
# ==============================================================================
# restore-fusion installer (replaces the installers of restore-tui AND btrfs-restore-tui)
#
#   sudo ./install.sh [--yes] [--upgrade|--force] [--no-venv] [--no-deps]
#
# What it does
#   1. Installs the app to /opt/restore-fusion (+ private venv) and the commands
#      restore-tui / restore-now / backup-now in /usr/local/bin.
#   2. RETIRES both old installs, if present, by ARCHIVING them (never deleting):
#        /opt/btrfs-restore-tui  and  /opt/restore-tui   -> /opt/restore-fusion-archive/<name>-<UTC time>/
#      together with their desktop entries and any old command symlink that pointed into them.
#   3. Seeds ~/.config/restore-tui/config.conf with mode 600 ONLY if no config exists.
#
# What it never does
#   * touch your backups (USB, SSH host) or local snapshots (/.snapshots): it does not even read them;
#   * overwrite or move an existing config.conf. If an existing config is readable by others (mode
#     looser than 600) it ASKS before tightening it (default No; --yes means No);
#   * replace a /usr/local/bin command that is not one of ours (it warns and leaves it).
# Backups made by either old edition keep working: the fused app reads both layouts.
#
# Test/packaging switches (environment):
#   DESTDIR=/some/dir    install under that prefix; no root needed, no chown, no package installs
#   RF_USER, RF_HOME     user and home to seed config for (default: SUDO_USER and its passwd home)
#   RF_TERMINAL=<cmd>    terminal used by the desktop entry (default: first of foot, gnome-terminal,
#                        konsole, kitty, alacritty, xterm found)
# ==============================================================================
set -euo pipefail

APP="restore-fusion"
DESTDIR="${DESTDIR:-}"
DESTDIR="${DESTDIR%/}"
OPT_DIR="/opt/${APP}"
BIN_DIR="/usr/local/bin"
APPS_DIR="/usr/share/applications"
ARCHIVE_DIR="/opt/${APP}-archive"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMANDS=(restore-tui restore-now backup-now)
OLD_APPS=(btrfs-restore-tui restore-tui)
OLD_DESKTOPS=("btrfs-restore.desktop:btrfs-restore-tui" "restore-tui.desktop:restore-tui")

ASSUME_YES=0; FORCE=0; NO_VENV=0; NO_DEPS=0
for a in "$@"; do
    case "$a" in
        --yes|-y) ASSUME_YES=1 ;;
        --upgrade|--force) FORCE=1 ;;
        --no-venv) NO_VENV=1 ;;
        --no-deps) NO_DEPS=1 ;;
        -h|--help) sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//' | head -n -1; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

if [ -z "${DESTDIR}" ] && [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi
[ -n "${DESTDIR}" ] && NO_DEPS=1   # never touch the host's packages from a staged install

REAL_USER="${RF_USER:-${SUDO_USER:-$(id -un)}}"
REAL_HOME="${RF_HOME:-$(getent passwd "${REAL_USER}" | cut -d: -f6)}"
[ -n "${REAL_HOME}" ] || { echo "[!] cannot find the home of ${REAL_USER}" >&2; exit 1; }

# Path inside the target system -> path on disk (differs only with DESTDIR)
D() { printf '%s%s' "${DESTDIR}" "$1"; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OWNED=()        # absolute (system) paths this install created: read by uninstall.sh
WARNINGS=()

if [ ! -f "${SRC_DIR}/restore_fusion/cli.py" ]; then
    echo "[!] ${SRC_DIR} does not look like a restore-tui-fusion checkout (restore_fusion/cli.py missing)" >&2
    exit 1
fi

_ask() {  # _ask "question" -> 0 only for an explicit yes. No tty or --yes => No.
    [ "${ASSUME_YES}" -eq 1 ] && return 1
    [ -t 0 ] || return 1
    local ans; read -r -p "$1 [y/N] " ans || return 1
    [[ "${ans}" =~ ^[Yy]$ ]]
}

_chown_user() {  # only meaningful for a real root install
    [ -z "${DESTDIR}" ] && chown "${REAL_USER}:" "$@" 2>/dev/null || true
}

echo "=== Installing ${APP} ==="

# --- 1. retire the old installs (archive, never delete) ------------------------------------------
_archive_move() {  # _archive_move <system path> <archive subdir name>
    local src dst
    src="$(D "$1")"; dst="$(D "${ARCHIVE_DIR}")/$2-${STAMP}"
    [ -e "${src}" ] || [ -L "${src}" ] || return 0
    mkdir -p "$(D "${ARCHIVE_DIR}")"
    if [ -e "${dst}" ]; then echo "[!] archive target ${dst} exists - leaving $1 alone" >&2; WARNINGS+=("not archived: $1"); return 0; fi
    mv -- "${src}" "${dst}"
    echo "    archived $1 -> ${ARCHIVE_DIR}/$2-${STAMP}"
}

retire_old_installs() {
    local name
    for name in "${OLD_APPS[@]}"; do
        local dir; dir="$(D "/opt/${name}")"
        if [ -d "${dir}" ] && [ ! -L "${dir}" ]; then
            echo "--> retiring the old install /opt/${name}"
            _archive_move "/opt/${name}" "${name}"
        fi
    done
    local pair file owner
    for pair in "${OLD_DESKTOPS[@]}"; do
        file="${pair%%:*}"; owner="${pair##*:}"
        if [ -f "$(D "${APPS_DIR}/${file}")" ]; then
            mkdir -p "$(D "${ARCHIVE_DIR}")/${owner}-desktop-${STAMP}"
            mv -- "$(D "${APPS_DIR}/${file}")" "$(D "${ARCHIVE_DIR}")/${owner}-desktop-${STAMP}/${file}"
            echo "    archived desktop entry ${file}"
        fi
    done
    # The btrfs edition's loose ~/.local/bin/backup-now (unversioned script) shadows ours on PATH.
    local legacy; legacy="$(D "${REAL_HOME}")/.local/bin/backup-now"
    if [ -f "${legacy}" ] && [ ! -L "${legacy}" ] && ! grep -q "restore_fusion" "${legacy}" 2>/dev/null; then
        mkdir -p "$(D "${ARCHIVE_DIR}")/legacy-backup-now-${STAMP}"
        mv -- "${legacy}" "$(D "${ARCHIVE_DIR}")/legacy-backup-now-${STAMP}/backup-now"
        echo "    archived the loose script ${REAL_HOME}/.local/bin/backup-now"
    fi
}

# --- 2. copy the app --------------------------------------------------------------------------------------
copy_app() {
    local dst; dst="$(D "${OPT_DIR}")"
    echo "--> ${OPT_DIR}"
    mkdir -p "${dst}"
    # only OUR code directories are replaced (not the venv, not anything else under /opt/restore-fusion)
    local sub
    for sub in restore_fusion bin; do rm -rf -- "${dst:?}/${sub}"; done
    tar -C "${SRC_DIR}" --exclude='__pycache__' --exclude='*.pyc' -cf - restore_fusion bin \
        | tar -C "${dst}" -xf -
    cp -- "${SRC_DIR}/requirements.txt" "${SRC_DIR}/config.conf.example" "${SRC_DIR}/uninstall.sh" "${dst}/"
    chmod +x "${dst}/bin/"* "${dst}/uninstall.sh"
    # commit installed, so the app can tell when a newer one exists (see restore_fusion/update.py)
    local rev; rev="$(git -c safe.directory="${SRC_DIR}" -C "${SRC_DIR}" rev-parse HEAD 2>/dev/null || true)"
    printf '%s\n' "${rev:-unknown}" > "${dst}/VERSION"
    OWNED+=("${OPT_DIR}")
}

build_venv() {
    local dst venv; dst="$(D "${OPT_DIR}")"; venv="${dst}/.venv"
    [ "${NO_VENV}" -eq 1 ] && { echo "--> venv skipped (--no-venv): the commands will use system python"; return 0; }
    if [ "${FORCE}" -eq 1 ] && [ -d "${venv}" ]; then
        echo "--> rebuilding ${OPT_DIR}/.venv"
        rm -rf -- "${venv}"
    fi
    [ -d "${venv}" ] && return 0
    if ! python3 -c 'import venv' 2>/dev/null; then
        echo "[!] python3 'venv' module missing (Debian/Ubuntu: apt install python3-venv)."
        echo "    Skipping the venv; the commands will use system python packages instead."
        WARNINGS+=("no venv: python3-venv missing")
        return 0
    fi
    if python3 -m venv "${venv}" \
       && "${venv}/bin/pip" install --quiet --upgrade pip \
       && "${venv}/bin/pip" install --quiet -r "${dst}/requirements.txt"; then
        echo "--> ${OPT_DIR}/.venv  ($(tr '\n' ' ' < "${dst}/requirements.txt"))"
    else
        echo "[!] could not build the venv (offline?). Removing the half-built one;"
        echo "    the commands fall back to system python. Re-run with --force when online."
        rm -rf -- "${venv}"
        WARNINGS+=("venv not built (offline?)")
    fi
}

install_sshfs() {
    [ "${NO_DEPS}" -eq 1 ] && return 0
    command -v sshfs >/dev/null 2>&1 && return 0
    echo "--> installing sshfs (browse a remote backup without downloading it)"
    if command -v pacman >/dev/null 2>&1; then pacman -S --needed --noconfirm sshfs || WARNINGS+=("sshfs not installed")
    elif command -v apt-get >/dev/null 2>&1; then apt-get install -y sshfs || WARNINGS+=("sshfs not installed")
    elif command -v dnf >/dev/null 2>&1; then dnf install -y fuse-sshfs || WARNINGS+=("sshfs not installed")
    else WARNINGS+=("install 'sshfs' yourself for lightweight remote restore"); fi
}

# --- 3. commands ----------------------------------------------------------------------------------------------
_is_ours_or_old() {  # is the existing symlink one of ours / of the two old editions?
    local t; t="$(readlink "$1" 2>/dev/null || true)"
    t="${t#"${DESTDIR}"}"
    case "${t}" in /opt/restore-fusion/*|/opt/btrfs-restore-tui/*|/opt/restore-tui/*) return 0 ;; esac
    return 1
}

link_commands() {
    mkdir -p "$(D "${BIN_DIR}")"
    echo "--> ${BIN_DIR}/{restore-tui,restore-now,backup-now}"
    local c tgt
    for c in "${COMMANDS[@]}"; do
        tgt="$(D "${BIN_DIR}/${c}")"
        if [ -L "${tgt}" ] && _is_ours_or_old "${tgt}"; then
            ln -sfn "${OPT_DIR}/bin/${c}" "${tgt}"
        elif [ ! -e "${tgt}" ] && [ ! -L "${tgt}" ]; then
            ln -s "${OPT_DIR}/bin/${c}" "${tgt}"
        else
            echo "[!] ${BIN_DIR}/${c} exists and is not ours - left in place (remove it to use ${APP})" >&2
            WARNINGS+=("${BIN_DIR}/${c} is not ours - left as is")
            continue
        fi
        OWNED+=("${BIN_DIR}/${c}")
    done
}

# --- 4. config (mode 600, only when none exists) ---------------------------------------------------------------
seed_config() {
    local cfgdir cfg c existing=""
    cfgdir="$(D "${REAL_HOME}")/.config/restore-tui"
    cfg="${cfgdir}/config.conf"
    for c in "${cfgdir}/config.conf" "$(D "${REAL_HOME}")/.config/btrfs-restore/config.conf"; do
        [ -f "${c}" ] && { existing="${c}"; break; }
    done
    if [ -z "${existing}" ]; then
        echo "--> seeding ${REAL_HOME}/.config/restore-tui/config.conf (mode 600)"
        mkdir -p "${cfgdir}"; chmod 700 "${cfgdir}"
        install -m 600 "${SRC_DIR}/config.conf.example" "${cfg}"
        _chown_user "${cfgdir}" "${cfg}"
        return 0
    fi
    echo "--> keeping your existing config: ${existing#"${DESTDIR}"} (not modified)"
    local mode; mode="$(stat -c '%a' "${existing}")"
    if [ "${mode}" != "600" ] && [ "${mode}" != "400" ]; then
        echo "    it is mode ${mode}; it may later hold PBS_PASSWORD."
        if _ask "    Tighten it to mode 600 now?"; then
            chmod 600 "${existing}"; echo "    set to 600"
        else
            echo "    left as is. Do it yourself with:  chmod 600 '${existing#"${DESTDIR}"}'"
        fi
    fi
}

# --- 5. desktop entry ------------------------------------------------------------------------------------------------
write_desktop() {
    local term="${RF_TERMINAL:-}" t exec_line
    if [ -z "${term}" ]; then
        for t in foot gnome-terminal konsole kitty alacritty xterm; do
            command -v "${t}" >/dev/null 2>&1 && { term="${t}"; break; }
        done
    fi
    case "${term}" in
        "") echo "    no terminal emulator found: skipping the desktop entry (run 'restore-tui' in a terminal)"; return 0 ;;
        foot) exec_line='foot --title="Restore TUI" restore-tui' ;;
        gnome-terminal) exec_line='gnome-terminal -- restore-tui' ;;
        konsole|alacritty|xterm) exec_line="${term} -e restore-tui" ;;
        *) exec_line="${term} restore-tui" ;;
    esac
    local f; f="$(D "${APPS_DIR}/${APP}.desktop")"
    mkdir -p "$(dirname "${f}")"
    echo "--> ${APPS_DIR}/${APP}.desktop (${term})"
    cat > "${f}" << EOF
[Desktop Entry]
Type=Application
Name=Restore TUI
Comment=Backup and granular restore (ext4 rsync / btrfs send-receive)
Exec=${exec_line}
Icon=drive-harddisk
Terminal=false
Categories=System;Archiving;Utility;
Keywords=backup;restore;snapshot;timemachine;
EOF
    chmod 644 "${f}"
    OWNED+=("${APPS_DIR}/${APP}.desktop")
}

record_owned() {  # what uninstall.sh may remove: exactly these, nothing else
    local f; f="$(D "${OPT_DIR}")/.owned-paths"
    printf '%s\n' "${OWNED[@]}" | sort -u > "${f}"
    chmod 644 "${f}"
}

retire_old_installs
copy_app
build_venv
install_sshfs
link_commands
seed_config
write_desktop
record_owned

echo
echo "=== Done ==="
echo "  restore-tui           menu: backup / restore / PBS / settings / recovery"
echo "  backup-now            incremental backup (engine chosen automatically)"
echo "  restore-now           retro TUI to browse & recover files"
echo "  restore-tui --gc      clear staging / scratch left by dead runs"
echo "  restore-tui --paths   list every path the tool touches"
echo "  sudo ${OPT_DIR}/uninstall.sh   remove it (keeps config, snapshots and the archive)"
if [ -d "$(D "${ARCHIVE_DIR}")" ]; then
    echo "  old installs archived in ${ARCHIVE_DIR}/ - delete that folder yourself once you are happy."
fi
echo "  Your backups (USB / SSH host) and /.snapshots were not touched."
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    echo; echo "Warnings:"; printf '  - %s\n' "${WARNINGS[@]}"
fi
