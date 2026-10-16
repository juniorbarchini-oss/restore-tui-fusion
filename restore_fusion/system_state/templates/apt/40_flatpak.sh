if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null 2>&1; then
    echo -e "${BLUE}[3/5] Flatpak applications...${NC}"
    while IFS= read -r app; do
        [ -n "$app" ] && flatpak install -y flathub "$app" 2>/dev/null || true
    done < "${STATE_DIR}/flatpak_list.txt"
fi

