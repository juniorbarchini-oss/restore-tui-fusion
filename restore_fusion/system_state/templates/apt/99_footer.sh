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
