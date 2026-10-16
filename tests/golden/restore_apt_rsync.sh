#!/usr/bin/env bash
# ==============================================================================
# restore-tui - Bare-Metal Disaster Recovery
# Generated automatically during backup. Run this from inside the snapshot folder.
# ==============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/_system_state"
HOME_SOURCE="${SCRIPT_DIR}/home"

# Lines for the "software to install" checklist printed at the end - not an
# error report. Some things (Chrome/Tailscale-style logins, Ubuntu Pro/ESM
# packages, apps installed from a standalone .deb, snaps, this app's own
# /opt install) can't be safely reinstalled unattended, so instead of guessing
# we just hand back a clean shopping list, same idea as reinstalling on a Mac
# and grabbing your apps again from the App Store.
TODO=()

echo -e "${GREEN}=======================================================${NC}"
echo -e "${GREEN}      restore-tui - BARE-METAL DISASTER RESTORE         ${NC}"
echo -e "${GREEN}=======================================================${NC}"
echo -e "Snapshot: ${SCRIPT_DIR}"
echo -e "Target home: ${HOME}"
echo ""
read -r -p "Perform a full system restore from this snapshot? (yes/no): " CONFIRM
[[ "${CONFIRM}" == "yes" ]] || { echo -e "${YELLOW}Aborted.${NC}"; exit 0; }

if [ -d "${STATE_DIR}/apt_sources" ]; then
    echo -e "${BLUE}[1/5] APT repositories & keyrings...${NC}"
    [ -d "${STATE_DIR}/apt_sources/keyrings" ] && sudo cp -n "${STATE_DIR}/apt_sources/keyrings/"* /etc/apt/keyrings/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/trusted.gpg.d" ] && sudo cp -n "${STATE_DIR}/apt_sources/trusted.gpg.d/"* /etc/apt/trusted.gpg.d/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/sources.list.d" ] && sudo cp -n "${STATE_DIR}/apt_sources/sources.list.d/"* /etc/apt/sources.list.d/ 2>/dev/null || true
    [ -d "${STATE_DIR}/apt_sources/usr_share_keyrings" ] && sudo mkdir -p /usr/share/keyrings && sudo cp -n "${STATE_DIR}/apt_sources/usr_share_keyrings/"* /usr/share/keyrings/ 2>/dev/null || true
    sudo apt-get update -y || true
fi

if [ -f "${STATE_DIR}/apt_packages_manual.txt" ]; then
    echo -e "${BLUE}[2/5] Manual APT packages...${NC}"
    # One at a time, on purpose: apt-get refuses the WHOLE batch the moment a
    # single name is entirely unknown (a .deb-only app, say) - --ignore-missing
    # only covers a known package that fails to download, not this. A single
    # bad name in a 130-package bulk install would otherwise silently install
    # nothing at all.
    while IFS= read -r pkg; do
        [ -n "$pkg" ] || continue
        if sudo apt-get install -y "$pkg" >/dev/null 2>&1; then
            continue
        fi
        # A couple of apt-only names have a well-known Snap Store equivalent -
        # confirmed working in the older LinuxBackup tool's own recovery flow.
        case "$pkg" in
            obsidian) sudo snap install obsidian --classic >/dev/null 2>&1 && continue ;;
        esac
        TODO+=("apt install $pkg")
    done < "${STATE_DIR}/apt_packages_manual.txt"
fi

if [ -f "${STATE_DIR}/snap_list.txt" ]; then
    # Best-effort auto-install, one at a time (--classic is a no-op error for
    # a strict-confinement snap, just a failed attempt, not a broken system -
    # LinuxBackup's own recovery does the same and it reliably brings back
    # most-to-all real apps). Skip OS runtime/base snaps - they reappear on
    # their own as dependencies and installing them directly is meaningless.
    while IFS= read -r line; do
        name="$(awk '{print $1}' <<<"$line")"
        case "$name" in
            ""|Name|bare|core|core1*|core2*|core3*|snapd|snapd-desktop-integration| \
            gtk-common-themes|desktop-security-center|firmware-updater| \
            prompting-client|snap-store|gnome-*|mesa-*|gaming-graphics-*)
                continue ;;
        esac
        sudo snap install "$name" --classic >/dev/null 2>&1 || TODO+=("snap install $name")
    done < "${STATE_DIR}/snap_list.txt"
fi

if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null 2>&1; then
    echo -e "${BLUE}[3/5] Flatpak applications...${NC}"
    while IFS= read -r app; do
        [ -n "$app" ] && flatpak install -y flathub "$app" 2>/dev/null || true
    done < "${STATE_DIR}/flatpak_list.txt"
fi

if [ -d "${HOME_SOURCE}" ]; then
    echo -e "${BLUE}[4/5] Home files (~)...${NC}"
    rsync -aAX --numeric-ids --info=progress2 "${HOME_SOURCE}/" "${HOME}/"
fi

if [ -f "${STATE_DIR}/dconf_dump.ini" ] && command -v dconf >/dev/null 2>&1; then
    echo -e "${BLUE}[5/5] GNOME desktop settings (dconf)...${NC}"
    dconf load / < "${STATE_DIR}/dconf_dump.ini" || true
fi

if [ -d "${STATE_DIR}/usr_local_bin" ]; then
    echo -e "${BLUE}[+] Custom /usr/local/bin scripts...${NC}"
    sudo cp -n "${STATE_DIR}/usr_local_bin/"* /usr/local/bin/ 2>/dev/null || true
fi

# [+] Re-install this app itself (and it alone - see the /usr/local/bin
# symlinks list above for other custom /opt-based tools) properly, via its
# own install.sh, if your dev clone came back with $HOME. Deliberately NOT
# "run it straight out of ~/Documents/desarrollo" - that was an older
# project's habit and it means a half-installed app with no real /opt/
# deployment or /usr/local/bin symlinks; this puts it back exactly where
# install.sh always puts it.
SELF_REINSTALLED=false
for _clone in "${HOME}/Documents/desarrollo/restore-tui" "${HOME}/Documents/desarrollo/alienback"; do
    if [ -f "${_clone}/restore_tui/cli.py" ] && [ -x "${_clone}/install.sh" ]; then
        echo -e "${BLUE}[+] Reinstalling restore-tui from ${_clone}...${NC}"
        if sudo "${_clone}/install.sh"; then
            SELF_REINSTALLED=true
        else
            TODO+=("re-run sudo ${_clone}/install.sh yourself")
        fi
        break
    fi
done

if [ -f "${STATE_DIR}/usr_local_bin_symlinks.txt" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        if [ "$SELF_REINSTALLED" = true ] && [[ "$line" == *"/opt/restore-tui/"* ]]; then
            continue  # install.sh above already put this one back
        fi
        TODO+=("reinstall: $line")
    done < "${STATE_DIR}/usr_local_bin_symlinks.txt"
fi

if [ -f "${STATE_DIR}/crontab.txt" ]; then
    echo -e "${BLUE}[+] User crontab...${NC}"
    crontab "${STATE_DIR}/crontab.txt" || true
fi

echo -e "${GREEN}=======================================================${NC}"
echo -e "${GREEN}    RESTORE COMPLETE - reboot recommended.              ${NC}"
echo -e "${GREEN}=======================================================${NC}"

if [ -f "${STATE_DIR}/gnome_extensions_enabled.txt" ]; then
    echo ""
    echo -e "${YELLOW}GNOME Shell extensions you had enabled (log out/in and${NC}"
    echo -e "${YELLOW}re-enable them from the Extensions app):${NC}"
    sed 's/^/  - /' "${STATE_DIR}/gnome_extensions_enabled.txt"
fi

if [ "${#TODO[@]}" -gt 0 ]; then
    echo ""
    echo -e "${YELLOW}=======================================================${NC}"
    echo -e "${YELLOW}  SOFTWARE TO INSTALL (grab these yourself, like an     ${NC}"
    echo -e "${YELLOW}  App Store shopping list - not an error):              ${NC}"
    echo -e "${YELLOW}=======================================================${NC}"
    printf '  - %s\n' "${TODO[@]}"
fi
