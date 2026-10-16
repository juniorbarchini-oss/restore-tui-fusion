if [ -f "${STATE_DIR}/dconf_dump.ini" ] && command -v dconf >/dev/null 2>&1; then
    echo -e "${BLUE}[5/5] GNOME desktop settings (dconf)...${NC}"
    dconf load / < "${STATE_DIR}/dconf_dump.ini" || true
fi

