#!/usr/bin/env bash
# ==============================================================================
# btrfs-restore-tui - Bare-Metal Disaster Recovery (Arch / Omarchy)
# Generated automatically during backup. Run from INSIDE this snapshot folder,
# on a freshly installed Arch base (booted, btrfs root, your user created), or
# from a live ISO with the new root mounted at /mnt (pass --root /mnt).
#
# Dependencies: bash, coreutils, rsync, pacman, btrfs-progs, zstd. Nothing else.
# ==============================================================================
set -euo pipefail

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/_system_state"
ROOT_PREFIX=""
DO_HOME=1; DO_PKGS=1; DO_ETC=1

while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT_PREFIX="${2%/}"; shift 2 ;;
        --no-home) DO_HOME=0; shift ;;
        --no-packages) DO_PKGS=0; shift ;;
        --no-etc) DO_ETC=0; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
done

# The home-tree step (rsync of root-owned files, chown -R) needs root. When we
# are restoring the booted system (no --root) and were not started with sudo,
# re-exec under sudo so [5/6] doesn't die on "Operation not permitted".
if [ -z "${ROOT_PREFIX}" ] && [ "$(id -u)" -ne 0 ]; then
    echo -e "${YELLOW}This recovery needs root - re-running with sudo...${NC}"
    exec sudo -E "$0" "$@"
fi

TARGET_USER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["user"])' "${STATE_DIR}/os_info.json" 2>/dev/null || echo "${SUDO_USER:-$USER}")"
TARGET_HOME="${ROOT_PREFIX}/home/${TARGET_USER}"

# home_<ts> is either a received btrfs subvolume or a .btrfs.zst stream
HOME_SRC=""
for d in "${SCRIPT_DIR}"/home_*; do [ -d "$d" ] && HOME_SRC="$d"; done
HOME_STREAM=""; [ -f "${SCRIPT_DIR}/home.btrfs.zst" ] && HOME_STREAM="${SCRIPT_DIR}/home.btrfs.zst"

echo -e "${GREEN}=================================================================${NC}"
echo -e "${GREEN}   btrfs-restore-tui - BARE-METAL RECOVERY (Arch / Omarchy)       ${NC}"
echo -e "${GREEN}=================================================================${NC}"
echo    "Snapshot : ${SCRIPT_DIR}"
echo    "User     : ${TARGET_USER}"
echo    "Home     : ${TARGET_HOME}"
[ -n "${ROOT_PREFIX}" ] && echo "Root pfx : ${ROOT_PREFIX}"
echo ""
read -r -p "Proceed with recovery from this snapshot? (yes/no): " C
[ "${C}" = "yes" ] || { echo -e "${YELLOW}Aborted.${NC}"; exit 0; }

run() { if [ -n "${ROOT_PREFIX}" ]; then arch-chroot "${ROOT_PREFIX}" "$@"; else sudo "$@"; fi; }

if [ "${DO_PKGS}" = 1 ] && command -v pacman >/dev/null; then
    echo -e "${BLUE}[1/6] pacman config & mirrors${NC}"
    if [ -d "${STATE_DIR}/pacman" ]; then
        [ -f "${STATE_DIR}/pacman/pacman.conf" ] && run cp "${STATE_DIR}/pacman/pacman.conf" /etc/pacman.conf || true
        [ -d "${STATE_DIR}/pacman/pacman.d" ] && run cp -rn "${STATE_DIR}/pacman/pacman.d/." /etc/pacman.d/ || true
    fi
    run pacman -Sy --noconfirm archlinux-keyring || true
    # Omarchy's 00-omarchy-update-guard.hook refuses a direct `pacman -Syu`
    # (it wants `omarchy update` instead) - opt back in for this one call, a
    # real recovery, not a casual upgrade. No-op on plain Arch.
    OMARCHY_BYPASS=()
    if command -v omarchy >/dev/null 2>&1 || grep -qi '^NAME="\?Omarchy' /etc/os-release 2>/dev/null; then
        OMARCHY_BYPASS=(env OMARCHY_ALLOW_DIRECT_PACMAN=1)
    fi
    run "${OMARCHY_BYPASS[@]}" pacman -Syu --noconfirm || true

    echo -e "${BLUE}[2/6] explicit packages${NC}"
    if [ -f "${STATE_DIR}/pkglist_explicit.txt" ]; then
        mapfile -t PKGS < "${STATE_DIR}/pkglist_explicit.txt"
        run pacman -S --needed --noconfirm "${PKGS[@]}" || \
            echo -e "${YELLOW}  some packages failed; re-run manually with --needed${NC}"
    fi

    echo -e "${BLUE}[3/6] AUR packages${NC}"
    if [ -f "${STATE_DIR}/pkglist_aur.txt" ] && [ -s "${STATE_DIR}/pkglist_aur.txt" ]; then
        AUR_HELPER=""
        AUR_EXTRA=()
        for h in yay paru; do command -v "$h" >/dev/null && AUR_HELPER="$h" && break; done
        # Never let the helper reach for a tty (edits/diffs/cleanup prompts) -
        # a recovery run may have none. yay and paru spell "don't ask" differently.
        case "${AUR_HELPER}" in
            yay)  AUR_EXTRA=(--answerclean None --answerdiff None --answeredit None --answerupgrade None) ;;
            paru) AUR_EXTRA=(--skipreview) ;;
        esac
        if [ -n "${AUR_HELPER}" ]; then
            sudo -u "${TARGET_USER}" "${AUR_HELPER}" -S --needed --noconfirm "${AUR_EXTRA[@]}" \
                - < "${STATE_DIR}/pkglist_aur.txt" || {
                echo -e "${YELLOW}  AUR install failed (some builds need a real terminal) - retry manually:${NC}"
                echo    "  ${AUR_HELPER} -S --needed - < ${STATE_DIR}/pkglist_aur.txt"
            }
        else
            echo -e "${YELLOW}  no AUR helper (yay/paru) - install one, then:${NC}"
            echo    "  yay -S --needed - < ${STATE_DIR}/pkglist_aur.txt"
        fi
    fi
else
    echo -e "${YELLOW}[1-3/6] packages skipped${NC}"
fi

echo -e "${BLUE}[4/6] Flatpaks${NC}"
if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null; then
    while IFS= read -r app; do [ -n "$app" ] && flatpak install -y flathub "$app" 2>/dev/null || true; done \
        < "${STATE_DIR}/flatpak_list.txt"
fi

if [ "${DO_HOME}" = 1 ]; then
    echo -e "${BLUE}[5/6] home tree -> ${TARGET_HOME}${NC}"
    mkdir -p "${TARGET_HOME}"
    if [ -n "${HOME_SRC}" ] && [ -d "${HOME_SRC}/${TARGET_USER}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/${TARGET_USER}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_SRC}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_STREAM}" ]; then
        STAGE="$(mktemp -d)"; echo "  receiving stream into ${STAGE}"
        zstd -dc "${HOME_STREAM}" | sudo btrfs receive "${STAGE}"
        SUB="$(find "${STAGE}" -maxdepth 1 -mindepth 1 -type d | head -1)"
        rsync -aAX --numeric-ids --info=progress2 "${SUB}/${TARGET_USER:-}/" "${TARGET_HOME}/" 2>/dev/null \
            || rsync -aAX --numeric-ids --info=progress2 "${SUB}/" "${TARGET_HOME}/"
        sudo btrfs subvolume delete "${SUB}" 2>/dev/null || true; rm -rf "${STAGE}"
    else
        echo -e "${YELLOW}  no home data in this snapshot${NC}"
    fi
    if id "${TARGET_USER}" >/dev/null 2>&1; then
        chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}" || true
    fi
fi

echo -e "${BLUE}[6/6] system config${NC}"
if [ "${DO_ETC}" = 1 ]; then
    [ -d "${STATE_DIR}/etc" ] && run cp -n "${STATE_DIR}/etc/." "${ROOT_PREFIX}/etc/" 2>/dev/null || true
    [ -d "${STATE_DIR}/keyd" ] && { run mkdir -p /etc/keyd; run cp -n "${STATE_DIR}/keyd/." /etc/keyd/ 2>/dev/null || true; }
    if [ -d "${STATE_DIR}/usr_local_bin" ]; then
        run mkdir -p /usr/local/bin
        run cp -n "${STATE_DIR}/usr_local_bin/." /usr/local/bin/ 2>/dev/null || true
    fi
    if [ -f "${STATE_DIR}/systemd_enabled.txt" ]; then
        awk '{print $1}' "${STATE_DIR}/systemd_enabled.txt" | while read -r u; do
            [ -n "$u" ] && run systemctl enable "$u" 2>/dev/null || true
        done
    fi
    [ -f "${STATE_DIR}/crontab.txt" ] && sudo -u "${TARGET_USER}" crontab "${STATE_DIR}/crontab.txt" || true
fi

echo -e "${GREEN}=================================================================${NC}"
echo -e "${GREEN}  RECOVERY COMPLETE${NC}"
echo -e "  - review /etc/fstab and the bootloader config under ${STATE_DIR}/bootloader/"
echo -e "  - regenerate initramfs if needed:  sudo mkinitcpio -P"
echo -e "  - reboot"
echo -e "${GREEN}=================================================================${NC}"
