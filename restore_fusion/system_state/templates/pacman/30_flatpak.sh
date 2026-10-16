echo -e "${BLUE}[4/6] Flatpaks${NC}"
if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null; then
    while IFS= read -r app; do [ -n "$app" ] && flatpak install -y flathub "$app" 2>/dev/null || true; done \
        < "${STATE_DIR}/flatpak_list.txt"
fi

