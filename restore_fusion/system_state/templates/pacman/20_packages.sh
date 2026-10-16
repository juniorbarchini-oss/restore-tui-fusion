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

