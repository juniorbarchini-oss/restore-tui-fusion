# [+] restore-fusion itself: if its dev clone came back with the home tree, reinstall it. Its
# install.sh retires the old restore-tui / btrfs-restore-tui installs.
# Linux keeps the clone in ~/desarrollo; ~/Documents/desarrollo is the macOS habit, kept as a fallback.
for FUSED_CLONE in "${TARGET_HOME}/desarrollo/restore-tui-fusion" "${TARGET_HOME}/Documents/desarrollo/restore-tui-fusion"; do
    if [ -z "${ROOT_PREFIX}" ] && [ -f "${FUSED_CLONE}/restore_fusion/cli.py" ] && [ -x "${FUSED_CLONE}/install.sh" ]; then
        echo -e "${BLUE}[+] Reinstalling restore-fusion from ${FUSED_CLONE}...${NC}"
        sudo "${FUSED_CLONE}/install.sh" || TODO+=("re-run sudo ${FUSED_CLONE}/install.sh yourself")
        break
    fi
done
