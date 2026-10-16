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

