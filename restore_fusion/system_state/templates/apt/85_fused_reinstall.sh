# [+] restore-fusion itself: if its dev clone came back with $HOME, reinstall it. Its install.sh
# retires the old restore-tui / btrfs-restore-tui installs, so this runs AFTER the block above.
# Linux keeps the clone in ~/desarrollo; ~/Documents/desarrollo is the macOS habit, kept as a fallback.
for FUSED_CLONE in "${HOME}/desarrollo/restore-tui-fusion" "${HOME}/Documents/desarrollo/restore-tui-fusion"; do
    if [ -f "${FUSED_CLONE}/restore_fusion/cli.py" ] && [ -x "${FUSED_CLONE}/install.sh" ]; then
        echo -e "${BLUE}[+] Reinstalling restore-fusion from ${FUSED_CLONE}...${NC}"
        sudo "${FUSED_CLONE}/install.sh" || TODO+=("re-run sudo ${FUSED_CLONE}/install.sh yourself")
        break
    fi
done
